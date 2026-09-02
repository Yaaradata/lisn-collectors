"""Cloud SQL conversation store for the diagnostic agent.

Cloud Run instances are ephemeral — conversation state must survive restarts.
Tables are agent_session / agent_message (see sql/agent_schema.sql).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import psycopg
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)


class SessionStore:
    """Read/write agent_* tables. Uses a write-capable DSN (AGENT_DSN)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def ensure_session(self, session_id: str) -> None:
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_session (session_id)
                    VALUES (%s)
                    ON CONFLICT (session_id) DO UPDATE
                      SET updated_at = now()
                    """,
                    (session_id,),
                )
            conn.commit()

    def load_messages(self, session_id: str) -> list[BaseMessage]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT role, content, payload
                    FROM agent_message
                    WHERE session_id = %s
                    ORDER BY message_id
                    """,
                    (session_id,),
                )
                rows = cur.fetchall()
        if not rows:
            return []
        payloads = []
        for row in rows:
            payload = row["payload"] or {}
            if not payload:
                payload = {
                    "type": row["role"],
                    "data": {"content": row["content"], "type": row["role"]},
                }
            payloads.append(payload)
        return messages_from_dict(payloads)

    def append_messages(self, session_id: str, messages: list[BaseMessage]) -> None:
        if not messages:
            return
        encoded = messages_to_dict(messages)
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_session (session_id)
                    VALUES (%s)
                    ON CONFLICT (session_id) DO UPDATE
                      SET updated_at = now()
                    """,
                    (session_id,),
                )
                for msg, payload in zip(messages, encoded, strict=True):
                    role = _role_of(msg)
                    content = _content_str(msg)
                    cur.execute(
                        """
                        INSERT INTO agent_message (session_id, role, content, payload)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (session_id, role, content, Jsonb(payload)),
                    )
            conn.commit()

    def history(self, session_id: str) -> list[dict[str, Any]]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM agent_session WHERE session_id = %s",
                    (session_id,),
                )
                if cur.fetchone() is None:
                    return []
                cur.execute(
                    """
                    SELECT message_id, role, content, payload, created_at
                    FROM agent_message
                    WHERE session_id = %s
                    ORDER BY message_id
                    """,
                    (session_id,),
                )
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = {
                "message_id": row["message_id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"].isoformat()
                if row["created_at"]
                else None,
            }
            payload = row["payload"] or {}
            data = payload.get("data") if isinstance(payload, dict) else {}
            if isinstance(data, dict) and data.get("tool_calls"):
                item["tool_calls"] = data["tool_calls"]
            if isinstance(data, dict) and data.get("tool_call_id"):
                item["tool_call_id"] = data["tool_call_id"]
                item["name"] = data.get("name")
            out.append(item)
        return out

    def delete_session(self, session_id: str) -> bool:
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM agent_session WHERE session_id = %s",
                    (session_id,),
                )
                deleted = cur.rowcount > 0
            conn.commit()
        return deleted

    def health_check(self) -> tuple[bool, str]:
        try:
            with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT to_regclass('public.agent_session') AS sessions,
                               to_regclass('public.agent_message') AS messages
                        """
                    )
                    row = cur.fetchone()
            if not row or not row["sessions"] or not row["messages"]:
                return (
                    False,
                    "agent_session/agent_message missing — apply "
                    "agent/backend/sql/agent_schema.sql",
                )
            return True, "agent_* tables present"
        except Exception as exc:  # noqa: BLE001
            logger.exception("session store health failed")
            return False, str(exc)


def _role_of(msg: BaseMessage) -> str:
    if isinstance(msg, HumanMessage):
        return "human"
    if isinstance(msg, AIMessage):
        return "ai"
    if isinstance(msg, ToolMessage):
        return "tool"
    if isinstance(msg, SystemMessage):
        return "system"
    # Fallback from type attribute.
    t = getattr(msg, "type", None) or "ai"
    if t in ("human", "ai", "tool", "system"):
        return str(t)
    return "ai"


def _content_str(msg: BaseMessage) -> str:
    content = msg.content
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, default=str)
    except TypeError:
        return str(content)
