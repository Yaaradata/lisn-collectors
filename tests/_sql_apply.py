"""Shared SQL helpers for discovery window / gap tests."""

from __future__ import annotations


def split_sql_statements(sql: str) -> list[str]:
    """Split SQL on ';' while respecting dollar-quotes and -- line comments."""
    out: list[str] = []
    buf: list[str] = []
    i = 0
    dollar_tag: str | None = None
    in_line_comment = False
    n = len(sql)
    while i < n:
        ch = sql[i]
        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            buf.append("--")
            i += 2
            in_line_comment = True
            continue
        if ch == "$":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if j < n and sql[j] == "$":
                dollar_tag = sql[i : j + 1]
                buf.append(dollar_tag)
                i = j + 1
                continue
        if ch == ";":
            buf.append(";")
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def apply_sql_file(cur, path: str) -> None:
    sql = open(path, encoding="utf-8").read()
    for stmt in split_sql_statements(sql):
        # Skip comment-only chunks.
        body = "\n".join(
            ln
            for ln in stmt.splitlines()
            if ln.strip() and not ln.strip().startswith("--")
        )
        if body:
            cur.execute(stmt)
