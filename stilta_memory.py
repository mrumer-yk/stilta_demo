from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from stilta_engine import redact_text, sha256_text, utc_now


ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "stilta_evidence_graph.sqlite"


DEFAULT_CLAIM = (
    "A system for monitoring industrial equipment, comprising: "
    "a sensor module configured to collect vibration data from the industrial equipment; "
    "a communication gateway configured to receive the vibration data from the sensor module; "
    "a processor configured to compare the vibration data against a threshold; "
    "and an alert interface configured to display a maintenance alert when the threshold is exceeded."
)

DEFAULT_SOURCES = [
    {
        "title": "Reference A - Monitoring Gateway Excerpt",
        "source_type": "prior_art",
        "text": (
            "The disclosed monitoring gateway receives vibration and temperature readings from remote sensor "
            "modules installed on pumps. The gateway forwards readings to an analytics service for processing."
        ),
    },
    {
        "title": "Reference B - Analytics Threshold Excerpt",
        "source_type": "prior_art",
        "text": (
            "The analytics service compares incoming vibration readings with configurable thresholds and records "
            "an equipment event when a threshold is exceeded."
        ),
    },
    {
        "title": "Reference C - Operator Dashboard Excerpt",
        "source_type": "product_doc",
        "text": (
            "A dashboard displays maintenance alerts for operators and stores historical equipment events. "
            "Operators can review pump status and acknowledge an alert after inspection."
        ),
    },
]


import contextlib


@contextlib.contextmanager
def connect():
    """Context manager that opens a SQLite connection, commits on success,
    rolls back on error, and always closes the connection."""
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS matters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                question TEXT NOT NULL DEFAULT '',
                target_patent TEXT NOT NULL DEFAULT '',
                priority_date TEXT NOT NULL DEFAULT '',
                claim_text TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matter_id INTEGER NOT NULL,
                thread_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS thread_summaries (
                thread_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                matter_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                source_type TEXT NOT NULL,
                text TEXT NOT NULL,
                redacted_text TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_chunks (
                id TEXT PRIMARY KEY,
                matter_id INTEGER NOT NULL,
                source_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                terms_json TEXT NOT NULL,
                hash TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS limitations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matter_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                text TEXT NOT NULL,
                interpretation TEXT NOT NULL,
                terms_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS evidence_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matter_id INTEGER NOT NULL,
                limitation_id TEXT NOT NULL,
                source_id TEXT,
                chunk_id TEXT,
                snippet TEXT NOT NULL,
                score REAL NOT NULL,
                support_level TEXT NOT NULL,
                overlap_terms_json TEXT NOT NULL,
                rationale TEXT NOT NULL,
                review_question TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS graph_nodes (
                id TEXT NOT NULL,
                matter_id INTEGER NOT NULL,
                node_type TEXT NOT NULL,
                label TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (matter_id, id)
            );

            CREATE TABLE IF NOT EXISTS graph_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matter_id INTEGER NOT NULL,
                from_node TEXT NOT NULL,
                to_node TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matter_id INTEGER NOT NULL,
                thread_id TEXT NOT NULL DEFAULT '',
                agent_name TEXT NOT NULL,
                status TEXT NOT NULL,
                input_json TEXT NOT NULL,
                output_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                input_json TEXT NOT NULL,
                output_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matter_id INTEGER NOT NULL,
                severity TEXT NOT NULL,
                warning_type TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                source_ref TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matter_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                html TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def seed_default_matter() -> None:
    init_db()
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM matters").fetchone()["count"]
    if count:
        return
    matter_id = create_matter(
        {
            "title": "Industrial Equipment Monitoring Review",
            "question": "Does the provided prior art disclose each limitation of claim 1?",
            "target_patent": "Internal review matter",
            "priority_date": "2020-01-15",
            "claim_text": DEFAULT_CLAIM,
        }
    )
    for source in DEFAULT_SOURCES:
        add_source(matter_id, source)
    record_audit(
        matter_id,
        "system",
        "Matter initialized",
        "Created the initial patent matter and source library.",
        {"source_count": len(DEFAULT_SOURCES)},
    )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def create_matter(payload: dict[str, Any]) -> int:
    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO matters (title, question, target_patent, priority_date, claim_text, summary, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, '', ?, ?)
            """,
            (
                str(payload.get("title") or "Untitled Patent Matter"),
                str(payload.get("question") or ""),
                str(payload.get("target_patent") or ""),
                str(payload.get("priority_date") or ""),
                str(payload.get("claim_text") or ""),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


def list_matters() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM matters ORDER BY updated_at DESC, id DESC").fetchall()
    return [dict(row) for row in rows]


def get_matter(matter_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM matters WHERE id = ?", (matter_id,)).fetchone()
    return row_to_dict(row)


def delete_matter(matter_id: int) -> bool:
    existing = get_matter(matter_id)
    if not existing:
        return False
    with connect() as conn:
        run_rows = conn.execute("SELECT id FROM agent_runs WHERE matter_id = ?", (matter_id,)).fetchall()
        run_ids = [row["id"] for row in run_rows]
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            conn.execute(f"DELETE FROM tool_calls WHERE run_id IN ({placeholders})", run_ids)
        for table in [
            "messages",
            "sources",
            "source_chunks",
            "limitations",
            "evidence_matches",
            "graph_nodes",
            "graph_edges",
            "agent_runs",
            "warnings",
            "reports",
        ]:
            conn.execute(f"DELETE FROM {table} WHERE matter_id = ?", (matter_id,))
        conn.execute("DELETE FROM memory_items WHERE scope = 'matter' AND scope_id = ?", (str(matter_id),))
        conn.execute("DELETE FROM matters WHERE id = ?", (matter_id,))
    return True


def update_matter(matter_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    existing = get_matter(matter_id)
    if not existing:
        raise ValueError("Matter not found")
    fields = ["title", "question", "target_patent", "priority_date", "claim_text"]
    values = {field: str(payload.get(field, existing.get(field) or "")) for field in fields}
    values["updated_at"] = utc_now()
    with connect() as conn:
        conn.execute(
            """
            UPDATE matters
            SET title = ?, question = ?, target_patent = ?, priority_date = ?, claim_text = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                values["title"],
                values["question"],
                values["target_patent"],
                values["priority_date"],
                values["claim_text"],
                values["updated_at"],
                matter_id,
            ),
        )
    return get_matter(matter_id) or {}


def next_source_id(matter_id: int) -> str:
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM sources WHERE matter_id = ?", (matter_id,)).fetchone()["count"]
    return f"M{matter_id}-SRC-{count + 1:03}"


def add_source(matter_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    source_id = str(payload.get("id") or next_source_id(matter_id))
    text = str(payload.get("text") or "")
    redacted, _ = redact_text(text)
    created_at = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO sources (id, matter_id, title, source_type, text, redacted_text, sha256, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                matter_id,
                str(payload.get("title") or source_id),
                str(payload.get("source_type") or "source"),
                text,
                redacted,
                sha256_text(text),
                created_at,
            ),
        )
        conn.execute("UPDATE matters SET updated_at = ? WHERE id = ?", (created_at, matter_id))
    clear_analysis(matter_id)
    record_audit(matter_id, "source", "Source saved", f"{source_id} added to the source library.", {"source_id": source_id})
    add_memory_item(
        "matter",
        str(matter_id),
        "source_summary",
        f"{source_id}: {str(payload.get('title') or source_id)} ({str(payload.get('source_type') or 'source')})",
        source_ref=source_id,
    )
    return get_source(source_id) or {}


def get_source(source_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return row_to_dict(row)


def list_sources(matter_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM sources WHERE matter_id = ? ORDER BY id", (matter_id,)).fetchall()
    return [dict(row) for row in rows]


def delete_source(matter_id: int, source_id: str) -> bool:
    """Remove a source from the library.  Clears any saved analysis so the
    claim chart is not left in a stale state referencing a deleted source."""
    existing = get_source(source_id)
    if not existing or existing["matter_id"] != matter_id:
        return False
    with connect() as conn:
        conn.execute("DELETE FROM sources WHERE id = ? AND matter_id = ?", (source_id, matter_id))
        conn.execute("UPDATE matters SET updated_at = ? WHERE id = ?", (utc_now(), matter_id))
    clear_analysis(matter_id)
    record_audit(
        matter_id,
        "source",
        "Source deleted",
        f"{source_id} removed from the source library. Analysis cleared.",
        {"source_id": source_id},
    )
    return True


def clear_analysis(matter_id: int) -> None:
    """Wipe all derived analysis tables for a matter so the UI reflects the
    current source library rather than a stale prior run."""
    with connect() as conn:
        for table in [
            "source_chunks",
            "limitations",
            "evidence_matches",
            "graph_nodes",
            "graph_edges",
            "warnings",
            "reports",
        ]:
            conn.execute(f"DELETE FROM {table} WHERE matter_id = ?", (matter_id,))


def save_analysis(matter_id: int, result: dict[str, Any], persist_audit_events: bool = True) -> None:
    now = utc_now()
    with connect() as conn:
        for table in [
            "source_chunks",
            "limitations",
            "evidence_matches",
            "graph_nodes",
            "graph_edges",
            "warnings",
        ]:
            conn.execute(f"DELETE FROM {table} WHERE matter_id = ?", (matter_id,))

        for chunk in result["chunks"]:
            conn.execute(
                """
                INSERT INTO source_chunks (id, matter_id, source_id, chunk_index, text, terms_json, hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk["id"],
                    matter_id,
                    chunk["source_id"],
                    chunk["chunk_index"],
                    chunk["text"],
                    json.dumps(chunk["terms"]),
                    chunk["hash"],
                ),
            )

        for limitation in result["limitations"]:
            conn.execute(
                """
                INSERT INTO limitations (matter_id, label, text, interpretation, terms_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    matter_id,
                    limitation["label"],
                    limitation["text"],
                    limitation["interpretation"],
                    json.dumps(limitation["terms"]),
                ),
            )

        for row in result["chart"]:
            conn.execute(
                """
                INSERT INTO evidence_matches (
                    matter_id, limitation_id, source_id, chunk_id, snippet, score, support_level,
                    overlap_terms_json, rationale, review_question
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    matter_id,
                    row["limitation_id"],
                    row.get("source_id"),
                    row.get("chunk_id"),
                    row.get("snippet") or "",
                    row.get("score") or 0,
                    row["support_level"],
                    json.dumps(row.get("overlap_terms", [])),
                    row["rationale"],
                    row["review_question"],
                ),
            )

        for node in result["graph"]["nodes"]:
            conn.execute(
                """
                INSERT INTO graph_nodes (id, matter_id, node_type, label, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (node["id"], matter_id, node["type"], node["label"], json.dumps(node)),
            )

        for edge in result["graph"]["edges"]:
            conn.execute(
                """
                INSERT INTO graph_edges (matter_id, from_node, to_node, edge_type, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (matter_id, edge["from"], edge["to"], edge["type"], json.dumps(edge)),
            )

        for warning in result["warnings"]:
            conn.execute(
                """
                INSERT INTO warnings (matter_id, severity, warning_type, message, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    matter_id,
                    warning["severity"],
                    warning["warning_type"],
                    warning["message"],
                    json.dumps(warning.get("payload", {})),
                    now,
                ),
            )

        conn.execute(
            "UPDATE matters SET summary = ?, updated_at = ? WHERE id = ?",
            (result["summary"], now, matter_id),
        )
        conn.execute(
            "INSERT INTO reports (matter_id, title, html, created_at) VALUES (?, ?, ?, ?)",
            (matter_id, "Attorney Review Packet", result["report_html"], now),
        )
    add_memory_item("matter", str(matter_id), "analysis_summary", result["summary"], confidence=0.82)
    add_memory_item(
        "matter",
        str(matter_id),
        "claim_chart_summary",
        f"{len(result['chart'])} claim-chart row(s), {len(result['warnings'])} warning(s), {len(result['chunks'])} source chunk(s).",
        confidence=0.9,
    )
    if result["warnings"]:
        add_memory_item(
            "matter",
            str(matter_id),
            "warning_summary",
            "; ".join(warning["message"] for warning in result["warnings"][:5]),
            confidence=0.75,
        )
    if persist_audit_events:
        for event in result["audit"]:
            record_audit(matter_id, event["event_type"], event["title"], event["body"], event)


def record_audit(matter_id: int, agent_name: str, title: str, body: str, payload: dict[str, Any] | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_runs (matter_id, thread_id, agent_name, status, input_json, output_json, created_at)
            VALUES (?, '', ?, 'completed', ?, ?, ?)
            """,
            (
                matter_id,
                agent_name,
                json.dumps({"title": title}),
                json.dumps({"body": body, "payload": payload or {}}),
                utc_now(),
            ),
        )


def start_agent_run(matter_id: int, agent_name: str, title: str, input_payload: dict[str, Any] | None = None) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO agent_runs (matter_id, thread_id, agent_name, status, input_json, output_json, created_at)
            VALUES (?, '', ?, 'running', ?, '{}', ?)
            """,
            (
                matter_id,
                agent_name,
                json.dumps({"title": title, "payload": input_payload or {}}),
                utc_now(),
            ),
        )
        return int(cursor.lastrowid)


def finish_agent_run(run_id: int, output_payload: dict[str, Any] | None = None, status: str = "completed") -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE agent_runs
            SET status = ?, output_json = ?
            WHERE id = ?
            """,
            (status, json.dumps(output_payload or {}), run_id),
        )


def fail_agent_run(run_id: int, error: Exception | str) -> None:
    finish_agent_run(run_id, {"error": str(error)}, status="failed")


def record_tool_call(
    run_id: int,
    tool_name: str,
    input_payload: dict[str, Any] | None = None,
    output_payload: dict[str, Any] | None = None,
    status: str = "completed",
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tool_calls (run_id, tool_name, input_json, output_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                tool_name,
                json.dumps(input_payload or {}),
                json.dumps(output_payload or {}),
                status,
                utc_now(),
            ),
        )


def add_memory_item(
    scope: str,
    scope_id: str,
    memory_type: str,
    content: str,
    confidence: float = 1.0,
    source_ref: str = "",
) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO memory_items (scope, scope_id, memory_type, content, confidence, source_ref, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (scope, scope_id, memory_type, content[:3000], float(confidence), source_ref, now, now),
        )


def add_message(matter_id: int, thread_id: str, role: str, content: str) -> dict[str, Any]:
    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (matter_id, thread_id, role, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (matter_id, thread_id, role, content, now),
        )
        conn.execute("UPDATE matters SET updated_at = ? WHERE id = ?", (now, matter_id))
        message_id = int(cursor.lastrowid)
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return dict(row)


def list_messages(matter_id: int, thread_id: str = "default") -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE matter_id = ? AND thread_id = ? ORDER BY id",
            (matter_id, thread_id),
        ).fetchall()
    return [dict(row) for row in rows]


def get_chart(matter_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM evidence_matches WHERE matter_id = ? ORDER BY id", (matter_id,)).fetchall()
    chart = []
    for row in rows:
        item = dict(row)
        item["terms"] = []
        item["overlap_terms"] = json.loads(item.pop("overlap_terms_json") or "[]")
        item["limitation_text"] = ""
        item["interpretation"] = ""
        chart.append(item)
    limitation_map = {item["label"]: item for item in get_limitations(matter_id)}
    for item in chart:
        limitation = limitation_map.get(item["limitation_id"], {})
        item["limitation_text"] = limitation.get("text", "")
        item["interpretation"] = limitation.get("interpretation", "")
        item["terms"] = limitation.get("terms", [])
    return chart


def get_limitations(matter_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM limitations WHERE matter_id = ? ORDER BY id", (matter_id,)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["terms"] = json.loads(item.pop("terms_json") or "[]")
        items.append(item)
    return items


def get_chunks(matter_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM source_chunks WHERE matter_id = ? ORDER BY source_id, chunk_index", (matter_id,)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["terms"] = json.loads(item.pop("terms_json") or "[]")
        items.append(item)
    return items


def get_warnings(matter_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM warnings WHERE matter_id = ? ORDER BY id", (matter_id,)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
        items.append(item)
    return items


def get_graph(matter_id: int) -> dict[str, Any]:
    with connect() as conn:
        node_rows = conn.execute(
            "SELECT * FROM graph_nodes WHERE matter_id = ? ORDER BY id", (matter_id,)
        ).fetchall()
        edge_rows = conn.execute(
            "SELECT * FROM graph_edges WHERE matter_id = ? ORDER BY id", (matter_id,)
        ).fetchall()
        nodes = [json.loads(row["payload_json"]) for row in node_rows]
        edges = [json.loads(row["payload_json"]) for row in edge_rows]
    return {"nodes": nodes, "edges": edges}



def get_audit(matter_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM agent_runs WHERE matter_id = ? ORDER BY id DESC LIMIT 40", (matter_id,)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["input"] = json.loads(item.pop("input_json") or "{}")
        item["output"] = json.loads(item.pop("output_json") or "{}")
        with connect() as conn:
            tool_rows = conn.execute("SELECT * FROM tool_calls WHERE run_id = ? ORDER BY id", (item["id"],)).fetchall()
        item["tool_calls"] = []
        for tool_row in tool_rows:
            tool = dict(tool_row)
            tool["input"] = json.loads(tool.pop("input_json") or "{}")
            tool["output"] = json.loads(tool.pop("output_json") or "{}")
            item["tool_calls"].append(tool)
        items.append(item)
    return items


def get_memory_items(matter_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM memory_items WHERE scope = 'matter' AND scope_id = ? ORDER BY id DESC LIMIT 40",
            (str(matter_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_latest_report(matter_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM reports WHERE matter_id = ? ORDER BY id DESC LIMIT 1", (matter_id,)).fetchone()
    return row_to_dict(row)


def get_full_matter(matter_id: int) -> dict[str, Any]:
    matter = get_matter(matter_id)
    if not matter:
        raise ValueError("Matter not found")
    return {
        "matter": matter,
        "sources": list_sources(matter_id),
        "limitations": get_limitations(matter_id),
        "chunks": get_chunks(matter_id),
        "chart": get_chart(matter_id),
        "warnings": get_warnings(matter_id),
        "graph": get_graph(matter_id),
        "messages": list_messages(matter_id),
        "audit": get_audit(matter_id),
        "memory": get_memory_items(matter_id),
        "report": get_latest_report(matter_id),
    }
