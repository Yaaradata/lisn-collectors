"""Graph wiring tests — stub model so we do not need live Vertex access.

clariversev1 currently returns 404 for Gemini publisher models in asia-south1
and us-central1. These tests prove the LangGraph loop, tool_calls reporting,
session persistence, and the 8-round cap without calling a real LLM.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.graph.agent import MAX_TOOL_ROUNDS, DiagnosticAgent
from app.graph.session import SessionStore


class ScriptedChatModel(BaseChatModel):
    """Returns a scripted sequence of AIMessages (tool call, then final)."""

    responses: list[AIMessage]
    i: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.i >= len(self.responses):
            msg = AIMessage(content="(script exhausted)")
        else:
            msg = self.responses[self.i]
            self.i += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        return self


@pytest.fixture
def scripted_agent(settings, tool_ctx, write_collector_dsn):
    sessions = SessionStore(write_collector_dsn)
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_diag_1",
                        "name": "diagnose_incident",
                        "args": {"incident_id": "IN270827PRECISION01"},
                    }
                ],
            ),
            AIMessage(
                content=(
                    "Verdict COLLECTED. Incident IN270827PRECISION01 is in "
                    "sentinel_core.incidents_current."
                )
            ),
        ]
    )
    return DiagnosticAgent(
        settings=settings,
        tool_ctx=tool_ctx,
        sessions=sessions,
        model=model,
    )


def test_chat_calls_diagnose_and_reports_tools(scripted_agent):
    sid = f"test-graph-{uuid.uuid4().hex[:8]}"
    result = scripted_agent.chat(
        sid, "Was incident IN270827PRECISION01 collected?"
    )
    assert result["session_id"] == sid
    assert result["tool_calls"], "tool_calls must be visible to the operator"
    assert result["tool_calls"][0]["name"] == "diagnose_incident"
    assert result["tool_calls"][0]["args"]["incident_id"] == "IN270827PRECISION01"
    assert "result" in result["tool_calls"][0]
    assert "COLLECTED" in result["reply"]
    assert result["stopped_early"] is False

    history = scripted_agent.sessions.history(sid)
    roles = [m["role"] for m in history]
    assert "human" in roles
    assert "ai" in roles
    assert "tool" in roles
    assert scripted_agent.sessions.delete_session(sid) is True


def test_tool_round_cap_stops_early(settings, tool_ctx, write_collector_dsn):
    # Model always requests another tool call — must stop after MAX_TOOL_ROUNDS.
    forever = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": f"call_{i}",
                        "name": "check_incident_collected",
                        "args": {"incident_id": "IN270827PRECISION01"},
                    }
                ],
            )
            for i in range(MAX_TOOL_ROUNDS + 5)
        ]
    )
    agent = DiagnosticAgent(
        settings=settings,
        tool_ctx=tool_ctx,
        sessions=SessionStore(write_collector_dsn),
        model=forever,
    )
    sid = f"test-cap-{uuid.uuid4().hex[:8]}"
    result = agent.chat(sid, "keep checking")
    assert result["stopped_early"] is True
    assert "Stopped early" in result["reply"]
    assert len(result["tool_calls"]) == MAX_TOOL_ROUNDS
    agent.sessions.delete_session(sid)
