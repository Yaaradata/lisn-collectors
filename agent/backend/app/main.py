"""FastAPI entrypoint for the LiSN collector diagnostic agent.

Read-only against Flipkart / collector / warehouse data. Chat sessions are
persisted in agent_* tables so Cloud Run restarts do not lose context.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.clients import HealthResult
from app.clients.bq import BigQueryClient
from app.clients.gcp import GcpRunClient
from app.clients.signoz import SignozClient
from app.clients.sql import SqlClient
from app.config import Settings, get_settings
from app.diagnostics import Diagnostics
from app.graph import DiagnosticAgent, SessionStore
from app.tools.base import ToolContext

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agent")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings

    sql = SqlClient(
        settings.collector_dsn_readonly,
        settings.sentinel_mock_dsn_readonly,
    )
    bq = BigQueryClient(
        project=settings.gcp_project,
        location=settings.gcp_region,
        max_bytes_billed=settings.bq_max_bytes_billed,
        raw_dataset=settings.bq_raw_dataset,
        core_dataset=settings.bq_core_dataset,
        landing_table=settings.bq_landing_table,
    )
    signoz = SignozClient(
        base_url=settings.signoz_base_url,
        api_key=settings.signoz_api_key,
    )
    gcp = GcpRunClient(
        project=settings.gcp_project,
        region=settings.gcp_region,
        job_names=settings.cloud_run_jobs,
    )
    diagnostics = Diagnostics(
        sql=sql, bq=bq, settings=settings, gcp=gcp
    )
    tool_ctx = ToolContext(
        sql=sql,
        bq=bq,
        signoz=signoz,
        gcp=gcp,
        settings=settings,
        diagnostics=diagnostics,
    )
    sessions = SessionStore(settings.resolve_agent_dsn())
    ok, msg = sessions.health_check()
    if not ok:
        logger.warning("session store: %s", msg)

    app.state.sql = sql
    app.state.bq = bq
    app.state.signoz = signoz
    app.state.gcp = gcp
    app.state.diagnostics = diagnostics
    app.state.tool_ctx = tool_ctx
    app.state.sessions = sessions
    app.state.agent = None
    try:
        app.state.agent = DiagnosticAgent(
            settings=settings, tool_ctx=tool_ctx, sessions=sessions
        )
    except Exception:
        # Allow diagnose endpoints to work even if the model cannot init
        # (missing Vertex ADC / Anthropic key). Chat will return 503.
        logger.exception(
            "diagnostic agent model failed to initialise "
            "(MODEL_PROVIDER=%s) — /v1/chat unavailable",
            settings.model_provider,
        )

    logger.info(
        "agent ready port=%s project=%s signoz_configured=%s model_provider=%s "
        "chat_ready=%s",
        settings.agent_port,
        settings.gcp_project,
        signoz.configured,
        settings.model_provider,
        app.state.agent is not None,
    )
    try:
        yield
    finally:
        sql.close()
        bq.close()
        signoz.close()
        gcp.close()


app = FastAPI(
    title="LiSN Collector Diagnostic Agent",
    description=(
        "Read-only operational diagnostics over Cloud SQL, BigQuery, "
        "SigNoz, and Cloud Run Jobs. Never writes collector data, never "
        "triggers collection."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "lisn-diagnostic-agent"}


@app.get("/health/sources")
def health_sources(request: Request) -> dict[str, object]:
    """Probe each source client so a broken dependency is visible without logs."""
    results: list[HealthResult] = [
        request.app.state.sql.health_check(),
        request.app.state.bq.health_check(),
        request.app.state.signoz.health_check(),
        request.app.state.gcp.health_check(),
    ]
    ok, msg = request.app.state.sessions.health_check()
    results.append(
        HealthResult(
            name="agent_sessions",
            status="ok" if ok else "error",
            message=msg,
        )
    )
    overall = "ok"
    for r in results:
        if r.status == "error":
            overall = "error"
            break
        if r.status == "unavailable" and overall == "ok":
            overall = "degraded"
    return {
        "status": overall,
        "sources": [r.model_dump() for r in results],
        "chat_ready": request.app.state.agent is not None,
        "model_provider": request.app.state.settings.model_provider,
    }


# These diagnose endpoints are useful on their own. If the chat interface is
# never built, they still answer the questions we have been answering by hand.


@app.get("/v1/diagnose/incident/{incident_id}")
def diagnose_incident(incident_id: str, request: Request) -> dict[str, object]:
    diag: Diagnostics = request.app.state.diagnostics
    try:
        return diag.diagnose_incident(incident_id).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/diagnose/range")
def diagnose_range(
    request: Request,
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
) -> dict[str, object]:
    diag: Diagnostics = request.app.state.diagnostics
    try:
        return diag.diagnose_time_range(from_, to).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/diagnose/gap")
def diagnose_gap(
    request: Request,
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
) -> dict[str, object]:
    diag: Diagnostics = request.app.state.diagnostics
    try:
        return diag.explain_gap(from_, to).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Chat — LangGraph tool-calling agent. No streaming yet: tool-call reporting
# stays simple; add streaming later if the UI needs it.
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    # An operator needs to see how an answer was reached. A black-box answer
    # about whether Flipkart data was collected is not trustworthy.
    tool_calls: list[dict[str, Any]]
    stopped_early: bool = False
    model_provider: str


@app.post("/v1/chat", response_model=ChatResponse)
def chat(body: ChatRequest, request: Request) -> ChatResponse:
    agent: DiagnosticAgent | None = request.app.state.agent
    if agent is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Chat model is not initialised. Check MODEL_PROVIDER / ADC / "
                f"ANTHROPIC_API_KEY (provider={request.app.state.settings.model_provider})."
            ),
        )
    try:
        result = agent.chat(body.session_id, body.message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ChatResponse(
        session_id=result["session_id"],
        reply=result["reply"],
        tool_calls=result["tool_calls"],
        stopped_early=bool(result.get("stopped_early")),
        model_provider=result["model_provider"],
    )


@app.get("/v1/chat/{session_id}/history")
def chat_history(session_id: str, request: Request) -> dict[str, object]:
    sessions: SessionStore = request.app.state.sessions
    return {
        "session_id": session_id,
        "messages": sessions.history(session_id),
    }


@app.delete("/v1/chat/{session_id}")
def chat_delete(session_id: str, request: Request) -> dict[str, object]:
    sessions: SessionStore = request.app.state.sessions
    deleted = sessions.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session_id": session_id, "deleted": True}


def run() -> None:
    settings: Settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.agent_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
