"""Minimal single-turn tool-calling agent.

Graph shape:
    START -> agent -> (tools -> agent)* -> END

Cap tool rounds at MAX_TOOL_ROUNDS. Exceeding that returns whatever answer
exists plus an explicit stopped-early note — forever-looping on a bad question
is worse than an incomplete answer.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.config import Settings
from app.graph.model import build_chat_model
from app.graph.prompt import SYSTEM_PROMPT
from app.graph.session import SessionStore
from app.graph.tools_bridge import build_langchain_tools
from app.redact import redact_tree
from app.tools.base import ToolContext

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8
STOPPED_EARLY_NOTE = (
    "\n\n[Stopped early: reached the maximum of "
    f"{MAX_TOOL_ROUNDS} tool rounds without a final answer. "
    "Rephrase the question or narrow the scope.]"
)


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    tool_rounds: int
    stopped_early: bool


class DiagnosticAgent:
    """Compile-once LangGraph runner with Cloud SQL session memory."""

    def __init__(
        self,
        *,
        settings: Settings,
        tool_ctx: ToolContext,
        sessions: SessionStore,
        model: BaseChatModel | None = None,
    ) -> None:
        self.settings = settings
        self.tool_ctx = tool_ctx
        self.sessions = sessions
        self.tools = build_langchain_tools(tool_ctx)
        base_model = model if model is not None else build_chat_model(settings)
        self.model = base_model.bind_tools(self.tools)
        self.graph = self._build_graph()

    def _build_graph(self) -> Any:
        model = self.model

        def agent_node(state: AgentState) -> dict[str, Any]:
            messages = list(state["messages"])
            if not messages or not isinstance(messages[0], SystemMessage):
                messages = [SystemMessage(content=SYSTEM_PROMPT), *messages]
            response = model.invoke(messages)
            return {"messages": [response]}

        def tools_node(state: AgentState) -> dict[str, Any]:
            # ToolNode executes whatever the model requested.
            result = ToolNode(self.tools).invoke(state)
            return {
                "messages": result["messages"],
                "tool_rounds": int(state.get("tool_rounds") or 0) + 1,
            }

        def stop_early_node(state: AgentState) -> dict[str, Any]:
            return {
                "messages": [
                    AIMessage(content=STOPPED_EARLY_NOTE.strip())
                ],
                "stopped_early": True,
            }

        def route_after_agent(
            state: AgentState,
        ) -> Literal["tools", "stop", "__end__"]:
            last = state["messages"][-1]
            tool_calls = getattr(last, "tool_calls", None) or []
            if not tool_calls:
                return "__end__"
            if int(state.get("tool_rounds") or 0) >= MAX_TOOL_ROUNDS:
                return "stop"
            return "tools"

        builder = StateGraph(AgentState)
        builder.add_node("agent", agent_node)
        builder.add_node("tools", tools_node)
        builder.add_node("stop", stop_early_node)
        builder.add_edge(START, "agent")
        builder.add_conditional_edges(
            "agent",
            route_after_agent,
            {"tools": "tools", "stop": "stop", "__end__": END},
        )
        builder.add_edge("tools", "agent")
        builder.add_edge("stop", END)
        return builder.compile()

    def chat(self, session_id: str, message: str) -> dict[str, Any]:
        session_id = session_id.strip()
        message = message.strip()
        if not session_id:
            raise ValueError("session_id is required")
        if not message:
            raise ValueError("message is required")

        history = self.sessions.load_messages(session_id)
        human = HumanMessage(content=message)
        prior_count = len(history)

        result = self.graph.invoke(
            {
                "messages": [
                    SystemMessage(content=SYSTEM_PROMPT),
                    *history,
                    human,
                ],
                "tool_rounds": 0,
                "stopped_early": False,
            },
            # Hard ceiling beyond the explicit tool-round counter.
            config={"recursion_limit": MAX_TOOL_ROUNDS * 2 + 4},
        )

        all_messages: list[BaseMessage] = list(result["messages"])
        # Persist only this turn's additions (skip the system prompt we inject).
        # history was prior user/ai/tool messages; new ones follow human.
        new_messages = _messages_after_history(all_messages, prior_count)
        self.sessions.append_messages(session_id, new_messages)

        reply, tool_calls = _extract_reply_and_tools(new_messages)
        if result.get("stopped_early") and STOPPED_EARLY_NOTE.strip() not in reply:
            reply = (reply or "").rstrip() + STOPPED_EARLY_NOTE

        usage = _token_usage(new_messages)
        return {
            "session_id": session_id,
            "reply": reply,
            # An operator needs to see how an answer was reached. A black-box
            # answer about whether Flipkart data was collected is not trustworthy.
            "tool_calls": tool_calls,
            "stopped_early": bool(result.get("stopped_early")),
            "model_provider": self.settings.model_provider,
            "usage": usage,
            "bq_bytes_scanned": int(
                getattr(self.tool_ctx.bq, "bytes_scanned_total", 0) or 0
            ),
        }


def _messages_after_history(
    all_messages: list[BaseMessage], prior_history_len: int
) -> list[BaseMessage]:
    """Drop the leading SystemMessage and previously persisted history."""
    # invoke input was [System, *history, human]; graph returns full list with
    # System retained at front when agent_node re-checks — take everything after
    # system + prior history.
    start = 1 + prior_history_len  # skip system + old history
    if start > len(all_messages):
        # Fallback: keep human + subsequent.
        return [m for m in all_messages if not isinstance(m, SystemMessage)]
    return all_messages[start:]


def _extract_reply_and_tools(
    messages: list[BaseMessage],
) -> tuple[str, list[dict[str, Any]]]:
    tool_calls: list[dict[str, Any]] = []
    results_by_id: dict[str, Any] = {}

    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for call in msg.tool_calls:
                tool_calls.append(
                    {
                        "id": call.get("id"),
                        "name": call.get("name"),
                        "args": redact_tree(call.get("args") or {}),
                    }
                )
        if msg.type == "tool":
            tcid = getattr(msg, "tool_call_id", None)
            try:
                parsed = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
            except (TypeError, json.JSONDecodeError):
                parsed = msg.content
            if tcid:
                results_by_id[tcid] = redact_tree(parsed)

    for call in tool_calls:
        cid = call.get("id")
        if cid and cid in results_by_id:
            call["result"] = results_by_id[cid]

    # Only report tool calls that actually ran (have a ToolMessage). An
    # AIMessage may request one more call that the round-cap then refuses.
    tool_calls = [c for c in tool_calls if "result" in c]

    # Final assistant text = last AIMessage without pending tool calls,
    # else last AIMessage content.
    reply = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            if msg.tool_calls and not (msg.content or "").strip():
                continue
            reply = _content_to_str(msg.content)
            if reply.strip():
                break
    return reply, tool_calls


def _token_usage(messages: list[BaseMessage]) -> dict[str, int]:
    """Sum usage_metadata across AIMessages in this turn."""
    in_tok = 0
    out_tok = 0
    total = 0
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        meta = getattr(msg, "usage_metadata", None) or {}
        if isinstance(meta, dict):
            in_tok += int(meta.get("input_tokens") or 0)
            out_tok += int(meta.get("output_tokens") or 0)
            total += int(meta.get("total_tokens") or 0)
        else:
            in_tok += int(getattr(meta, "input_tokens", 0) or 0)
            out_tok += int(getattr(meta, "output_tokens", 0) or 0)
            total += int(getattr(meta, "total_tokens", 0) or 0)
    if total == 0:
        total = in_tok + out_tok
    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": total,
    }


def _content_to_str(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)
