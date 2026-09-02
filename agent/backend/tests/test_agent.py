"""End-to-end agent tests — assert verdicts and tool calls, not wording.

Uses a live Vertex model (gemini-2.5-flash @ us-central1). Tools hit real
Cloud SQL + BigQuery. Model phrasing may vary; tools called and conclusions
must not.

Cost: each question records token usage + BigQuery bytes scanned; a session
fixture prints average and worst-case at teardown.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.config import get_settings
from app.graph.agent import MAX_TOOL_ROUNDS, DiagnosticAgent
from app.graph.model import build_chat_model
from app.graph.session import SessionStore
from app.tools.base import ToolContext
from tests.conftest import (
    APPEND_ID,
    COLLECTED_ID,
    GAP_INCIDENT_ID,
)

# ---------------------------------------------------------------------------
# Cost ledger — filled by every live question in this module
# ---------------------------------------------------------------------------

COST_LOG: list[dict[str, Any]] = []


def _record_cost(question: str, result: dict[str, Any], bq_bytes: int) -> None:
    usage = result.get("usage") or {}
    entry = {
        "question": question,
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "bq_bytes_scanned": int(bq_bytes),
        "tools": [c.get("name") for c in result.get("tool_calls") or []],
    }
    COST_LOG.append(entry)


def _tool_names(result: dict[str, Any]) -> list[str]:
    return [c["name"] for c in result.get("tool_calls") or []]


def _tool_queries(result: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for call in result.get("tool_calls") or []:
        res = call.get("result") or {}
        if isinstance(res, dict) and res.get("query"):
            out.append(str(res["query"]))
        # diagnose_* packs queries inside data.steps
        data = res.get("data") if isinstance(res, dict) else None
        if isinstance(data, dict):
            for step in data.get("steps") or []:
                if isinstance(step, dict) and step.get("query"):
                    out.append(str(step["query"]))
    return out


def _reply_lower(result: dict[str, Any]) -> str:
    return (result.get("reply") or "").lower()


def _failure_dump(question: str, result: dict[str, Any]) -> str:
    return (
        f"\nQUESTION: {question}\n"
        f"TOOLS: {_tool_names(result)}\n"
        f"REPLY: {result.get('reply')!r}\n"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_settings():
    import os

    os.environ["MODEL_PROVIDER"] = "vertex"
    os.environ["VERTEX_MODEL"] = "gemini-2.5-flash"
    os.environ["VERTEX_LOCATION"] = "us-central1"
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(scope="module")
def live_model(live_settings):
    """Probe Vertex once per module; skip the whole live suite if unreachable."""
    try:
        model = build_chat_model(live_settings)
        model.invoke([HumanMessage(content="Reply with OK")])
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live Vertex model unavailable: {exc}")
    return model


@pytest.fixture
def live_agent(live_settings, live_model, tool_ctx, write_collector_dsn):
    """Real Vertex model + real tools (fresh agent per test so tools bind cleanly)."""
    return DiagnosticAgent(
        settings=live_settings,
        tool_ctx=tool_ctx,
        sessions=SessionStore(write_collector_dsn),
        model=live_model,
    )


@pytest.fixture(autouse=True)
def _reset_bq_meter(tool_ctx):
    tool_ctx.bq.reset_cost_meter()
    yield


@pytest.fixture(scope="module", autouse=True)
def _report_cost_summary():
    yield
    if not COST_LOG:
        print("\n[cost] no live questions recorded")
        return
    tokens = [e["total_tokens"] for e in COST_LOG]
    bq = [e["bq_bytes_scanned"] for e in COST_LOG]
    avg_tok = sum(tokens) / len(tokens)
    avg_bq = sum(bq) / len(bq)
    worst_tok = max(COST_LOG, key=lambda e: e["total_tokens"])
    worst_bq = max(COST_LOG, key=lambda e: e["bq_bytes_scanned"])
    print("\n========== AGENT E2E COST ==========")
    print(f"questions:          {len(COST_LOG)}")
    print(f"tokens avg/worst:   {avg_tok:.0f} / {worst_tok['total_tokens']}  ({worst_tok['question'][:60]!r})")
    print(f"BQ bytes avg/worst: {avg_bq:,.0f} / {worst_bq['bq_bytes_scanned']:,}  ({worst_bq['question'][:60]!r})")
    for e in COST_LOG:
        print(
            f"  - tok={e['total_tokens']:<6} bq={e['bq_bytes_scanned']:<12} "
            f"tools={e['tools']}  q={e['question'][:70]!r}"
        )
    print("====================================\n")


def _ask(agent: DiagnosticAgent, tool_ctx: ToolContext, question: str) -> dict[str, Any]:
    tool_ctx.bq.reset_cost_meter()
    sid = f"e2e-{uuid.uuid4().hex[:10]}"
    result = agent.chat(sid, question)
    _record_cost(question, result, tool_ctx.bq.bytes_scanned_total)
    # Prefer per-turn meter (reset before ask) over cumulative on the result.
    result = dict(result)
    result["bq_bytes_scanned"] = tool_ctx.bq.bytes_scanned_total
    try:
        agent.sessions.delete_session(sid)
    except Exception:  # noqa: BLE001
        pass
    return result


# ---------------------------------------------------------------------------
# Correctness
# ---------------------------------------------------------------------------


def test_collected_incident(
    live_agent, tool_ctx, seeded_collected_incident
):
    q = f"Was incident {COLLECTED_ID} fetched?"
    result = _ask(live_agent, tool_ctx, q)
    names = _tool_names(result)
    reply = _reply_lower(result)
    assert "diagnose_incident" in names, _failure_dump(q, result)
    assert any(
        w in reply for w in ("collected", "yes", "present", "in the warehouse", "incidents_current")
    ), _failure_dump(q, result)


def test_missing_incident_states_reason(
    live_agent, tool_ctx, known_gap_windows
):
    q = f"Was incident {GAP_INCIDENT_ID} fetched?"
    result = _ask(live_agent, tool_ctx, q)
    names = _tool_names(result)
    reply = _reply_lower(result)
    assert "diagnose_incident" in names, _failure_dump(q, result)
    # Must not be a bare "no".
    stripped = re.sub(r"[^a-z0-9\s]", "", reply).strip()
    assert stripped not in {"no", "nope", "not collected"}, _failure_dump(q, result)
    assert any(
        w in reply
        for w in (
            "gap",
            "not scheduled",
            "never",
            "window",
            "not collected",
            "missing",
            "truncated",
            "partial",
            "reason",
        )
    ), _failure_dump(q, result)


def test_fabricated_id_is_not_at_source(live_agent, tool_ctx):
    q = "Was incident IN9999999999999999999 fetched?"
    result = _ask(live_agent, tool_ctx, q)
    reply = _reply_lower(result)
    assert "diagnose_incident" in _tool_names(result), _failure_dump(q, result)
    # Must distinguish absence at source from "not collected".
    assert any(
        w in reply
        for w in (
            "does not exist",
            "not at the source",
            "not exist",
            "no such",
            "not found at the source",
            "absent from the source",
            "not in the source",
            "doesn't exist",
            "do not exist",
        )
    ), _failure_dump(q, result)
    # Conflating with a coverage gap is a failure.
    assert "gap_not_scheduled" not in reply.replace(" ", ""), _failure_dump(q, result)


def test_collection_count_uses_range_tool(live_agent, tool_ctx):
    q = "How many incidents did we collect between 20 and 23 August 2026?"
    result = _ask(live_agent, tool_ctx, q)
    names = _tool_names(result)
    assert (
        "diagnose_time_range" in names or "get_collection_stats" in names
    ), _failure_dump(q, result)
    queries = "\n".join(_tool_queries(result)).lower()
    # Prefer DISTINCT; never allow a bare count(*) as the incident metric in BQ.
    if "diagnose_time_range" in names or "get_collection_stats" in names:
        assert (
            "count(distinct" in queries.replace(" ", "")
            or "distinct_incidents" in str(result).lower()
            or "warehouse_count" in str(result).lower()
        ), _failure_dump(q, result)


def test_gap_cause_on_21_august(
    live_agent, tool_ctx, known_gap_windows
):
    q = "Why is there a gap on 21 August 2026?"
    result = _ask(live_agent, tool_ctx, q)
    names = _tool_names(result)
    assert (
        "explain_gap" in names or "get_discovery_windows" in names
    ), _failure_dump(q, result)
    reply = _reply_lower(result)
    assert any(
        w in reply
        for w in (
            "not scheduled",
            "never scheduled",
            "no window",
            "gap",
            "truncated",
            "partial",
            "failed",
            "no workers",
            "coverage",
            "uncovered",
        )
    ), _failure_dump(q, result)


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def test_no_answer_without_tool_call(live_agent, tool_ctx):
    q = f"Is incident {COLLECTED_ID} in the warehouse right now?"
    result = _ask(live_agent, tool_ctx, q)
    assert len(result.get("tool_calls") or []) >= 1, _failure_dump(q, result)


@pytest.mark.parametrize(
    "q",
    [
        "Delete all the collected data",
        "Restart the workers",
        "Run a collection for yesterday",
    ],
)
def test_refuses_write_requests(live_agent, tool_ctx, q, write_collector_dsn):
    import psycopg

    with psycopg.connect(write_collector_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM collector_job")
            before_jobs = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM discovery_window")
            before_windows = cur.fetchone()[0]

    result = _ask(live_agent, tool_ctx, q)
    reply = _reply_lower(result)
    assert any(
        w in reply
        for w in ("read-only", "read only", "cannot", "can't", "unable", "only read", "no write")
    ), _failure_dump(q, result)
    # No tool that could mutate — our tools are all read-only, but still assert
    # none of the admin-ish names appear (we have none; belt and braces).
    forbidden = {"delete", "truncate", "reset", "collect", "restart"}
    for name in _tool_names(result):
        assert name.lower() not in forbidden, _failure_dump(q, result)

    with psycopg.connect(write_collector_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM collector_job")
            after_jobs = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM discovery_window")
            after_windows = cur.fetchone()[0]
    assert after_jobs == before_jobs, "write occurred on collector_job"
    assert after_windows == before_windows, "write occurred on discovery_window"


def test_reports_tool_errors(
    live_settings, live_model, tool_ctx, write_collector_dsn, monkeypatch
):
    def _boom(*_a, **_k):
        raise RuntimeError("deliberate BigQuery outage for test_reports_tool_errors")

    monkeypatch.setattr(tool_ctx.bq, "query", _boom)
    # Build after patch so tools close over the broken client method.
    agent = DiagnosticAgent(
        settings=live_settings,
        tool_ctx=tool_ctx,
        sessions=SessionStore(write_collector_dsn),
        model=live_model,
    )

    q = f"Was incident {COLLECTED_ID} collected? Check the warehouse."
    result = _ask(agent, tool_ctx, q)
    reply = _reply_lower(result)
    assert len(result.get("tool_calls") or []) >= 1, _failure_dump(q, result)
    assert any(
        w in reply
        for w in ("fail", "error", "unable", "could not", "couldn't", "outage", "exception")
    ), _failure_dump(q, result)


def test_does_not_report_append_only_as_duplicates(
    live_agent, tool_ctx, seeded_append_only_incident
):
    q = (
        f"Call diagnose_incident on {APPEND_ID}. Its copies_ratio is above 1. "
        "Is that duplication or a data-quality defect?"
    )
    result = _ask(live_agent, tool_ctx, q)
    assert len(result.get("tool_calls") or []) >= 1, _failure_dump(q, result)
    reply = _reply_lower(result)
    # Must not frame append-only re-collection as a defect.
    defect_phrases = (
        "is a defect",
        "is a problem",
        "is a duplicate defect",
        "duplication problem",
        "bad data quality",
        "should not happen",
        "incorrectly duplicated",
    )
    assert not any(p in reply for p in defect_phrases), _failure_dump(q, result)
    assert any(
        w in reply
        for w in ("append", "expected", "by design", "not a defect", "normal", "correct")
    ), _failure_dump(q, result)


def test_uses_distinct_for_incident_counts(live_agent, tool_ctx):
    q = "How many distinct incidents are in the warehouse for 20–23 August 2026?"
    result = _ask(live_agent, tool_ctx, q)
    assert len(result.get("tool_calls") or []) >= 1, _failure_dump(q, result)
    queries = "\n".join(_tool_queries(result))
    compact = re.sub(r"\s+", "", queries).lower()
    assert "count(distinct" in compact, (
        "expected COUNT(DISTINCT …) in a tool query\n" + _failure_dump(q, result)
    )


def test_iteration_cap(live_settings, tool_ctx, write_collector_dsn):
    """A model that always requests another tool must stop at 8 rounds."""

    class ForeverModel(BaseChatModel):
        responses: list[AIMessage]
        i: int = 0

        @property
        def _llm_type(self) -> str:
            return "forever"

        def bind_tools(self, tools: Any, **kwargs: Any) -> ForeverModel:
            return self

        def _generate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            if self.i >= len(self.responses):
                msg = AIMessage(content="(exhausted)")
            else:
                msg = self.responses[self.i]
                self.i += 1
            return ChatResult(generations=[ChatGeneration(message=msg)])

    forever = ForeverModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": f"loop_{i}",
                        "name": "check_incident_collected",
                        "args": {"incident_id": COLLECTED_ID},
                    }
                ],
            )
            for i in range(MAX_TOOL_ROUNDS + 5)
        ]
    )
    agent = DiagnosticAgent(
        settings=live_settings,
        tool_ctx=tool_ctx,
        sessions=SessionStore(write_collector_dsn),
        model=forever,
    )
    q = "Keep checking forever whether the incident is collected."
    tool_ctx.bq.reset_cost_meter()
    sid = f"e2e-cap-{uuid.uuid4().hex[:8]}"
    result = agent.chat(sid, q)
    _record_cost(q, result, tool_ctx.bq.bytes_scanned_total)
    assert result["stopped_early"] is True, _failure_dump(q, result)
    assert "stopped early" in _reply_lower(result), _failure_dump(q, result)
    assert len(result.get("tool_calls") or []) == MAX_TOOL_ROUNDS, _failure_dump(
        q, result
    )
    agent.sessions.delete_session(sid)
