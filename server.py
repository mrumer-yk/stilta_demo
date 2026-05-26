from __future__ import annotations

import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from gemini_client import gemini_client
from stilta_engine import (
    analyze_matter,
    answer_from_matter,
    build_claim_chart,
    build_graph,
    build_report_html,
    build_warnings,
    chunk_source,
    draft_grounded_summary,
    redact_text,
    split_claim_into_limitations,
    validate_citations,
)
from stilta_memory import (
    add_message,
    add_memory_item,
    add_source,
    clear_analysis,
    create_matter,
    delete_matter,
    delete_source,
    fail_agent_run,
    finish_agent_run,
    get_chart,
    get_chunks,
    get_full_matter,
    get_latest_report,
    get_matter,
    init_db,
    list_matters,
    list_sources,
    record_audit,
    record_tool_call,
    save_analysis,
    seed_default_matter,
    start_agent_run,
    update_matter,
)


ROOT = Path(__file__).parent
PUBLIC_DIR = ROOT / "public"


def json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if not length:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def parse_matter_path(path: str) -> tuple[int | None, str | None]:
    match = re.fullmatch(r"/api/matters/(\d+)(?:/([a-z-]+))?", path)
    if not match:
        return None, None
    return int(match.group(1)), match.group(2)


def parse_source_delete_path(path: str) -> tuple[int | None, str | None]:
    """Match DELETE /api/matters/{id}/sources/{source_id}"""
    match = re.fullmatch(r"/api/matters/(\d+)/sources/([A-Za-z0-9_\-]+)", path)
    if not match:
        return None, None
    return int(match.group(1)), match.group(2)


def build_state() -> dict[str, Any]:
    seed_default_matter()
    matters = list_matters()
    active_id = matters[0]["id"] if matters else None
    return {
        "matters": matters,
        "active_matter_id": active_id,
        "active": get_full_matter(active_id) if active_id else None,
        "model": gemini_client().status(),
    }


def run_analysis(matter_id: int) -> dict[str, Any]:
    matter = get_matter(matter_id)
    if not matter:
        raise ValueError("Matter not found")
    sources = list_sources(matter_id)
    gemini = gemini_client()
    controller_run = start_agent_run(
        matter_id,
        "analysis_controller",
        "Analysis started",
        {"source_count": len(sources), "gemini_enabled": gemini.enabled, "model": gemini.model},
    )
    try:
        clear_analysis(matter_id)
        redactions: list[dict[str, str]] = []

        claim_run = start_agent_run(
            matter_id,
            "claim_parser",
            "Parse claim limitations",
            {"claim_chars": len(matter.get("claim_text", "")), "gemini_enabled": gemini.enabled},
        )
        try:
            limitations = split_claim_into_limitations(matter.get("claim_text", ""), gemini)
            record_tool_call(
                claim_run,
                "split_claim_into_limitations",
                {"gemini_enabled": gemini.enabled, "model": gemini.model},
                {"limitation_count": len(limitations)},
            )
            finish_agent_run(
                claim_run,
                {
                    "title": "Claim parsed",
                    "body": f"{len(limitations)} limitation(s) generated.",
                    "payload": {"limitation_ids": [item["label"] for item in limitations]},
                },
            )
        except Exception as exc:
            fail_agent_run(claim_run, exc)
            raise

        corpus_run = start_agent_run(
            matter_id,
            "corpus",
            "Redact and chunk sources",
            {"source_count": len(sources)},
        )
        try:
            chunks = []
            safe_sources = []
            for source in sources:
                redacted, found = redact_text(source.get("text", ""))
                safe_source = {**source, "redacted_text": redacted}
                safe_sources.append(safe_source)
                redactions.extend(found)
                chunks.extend(chunk_source(safe_source))
            record_tool_call(
                corpus_run,
                "redact_text_and_chunk_source",
                {"source_count": len(sources)},
                {"chunk_count": len(chunks), "redaction_count": len(redactions)},
            )
            finish_agent_run(
                corpus_run,
                {
                    "title": "Sources chunked",
                    "body": f"{len(chunks)} source chunk(s) created.",
                    "payload": {"redaction_count": len(redactions)},
                },
            )
        except Exception as exc:
            fail_agent_run(corpus_run, exc)
            raise

        matcher_run = start_agent_run(
            matter_id,
            "matcher",
            "Match limitations to sources",
            {"limitation_count": len(limitations), "chunk_count": len(chunks), "gemini_enabled": gemini.enabled},
        )
        try:
            chart = build_claim_chart(limitations, chunks, gemini)
            record_tool_call(
                matcher_run,
                "build_claim_chart",
                {"gemini_enabled": gemini.enabled, "model": gemini.model},
                {
                    "row_count": len(chart),
                    "matched_count": sum(1 for row in chart if row.get("source_id")),
                    "ai_reviewed_count": sum(1 for row in chart if row.get("ai_review")),
                },
            )
            finish_agent_run(
                matcher_run,
                {
                    "title": "Evidence matched",
                    "body": f"{len(chart)} claim chart row(s) scored.",
                    "payload": {"matched_count": sum(1 for row in chart if row.get("source_id"))},
                },
            )
        except Exception as exc:
            fail_agent_run(matcher_run, exc)
            raise

        audit_run = start_agent_run(
            matter_id,
            "auditor",
            "Run citation and safety audit",
            {"chart_rows": len(chart)},
        )
        try:
            summary = draft_grounded_summary(chart)
            warnings = build_warnings(chart, summary)
            warnings.extend(validate_citations(summary, {source["id"] for source in sources}))
            if redactions:
                warnings.append(
                    {
                        "severity": "medium",
                        "warning_type": "redaction",
                        "message": f"{len(redactions)} sensitive item(s) redacted before analysis output.",
                        "payload": {"count": len(redactions), "types": sorted({item["type"] for item in redactions})},
                    }
                )
            record_tool_call(
                audit_run,
                "build_warnings_and_validate_citations",
                {"source_ids": [source["id"] for source in sources]},
                {"warning_count": len(warnings)},
            )
            finish_agent_run(
                audit_run,
                {
                    "title": "Citation audit",
                    "body": f"{len(warnings)} warning(s) generated.",
                    "payload": {"warning_types": sorted({warning["warning_type"] for warning in warnings})},
                },
            )
        except Exception as exc:
            fail_agent_run(audit_run, exc)
            raise

        graph_run = start_agent_run(
            matter_id,
            "graph_builder",
            "Build evidence graph",
            {"chart_rows": len(chart), "source_count": len(sources)},
        )
        try:
            graph = build_graph(int(matter["id"]), sources, chart)
            record_tool_call(
                graph_run,
                "build_graph",
                {"chart_rows": len(chart)},
                {"nodes": len(graph["nodes"]), "edges": len(graph["edges"])},
            )
            finish_agent_run(
                graph_run,
                {
                    "title": "Evidence graph built",
                    "body": f"{len(graph['nodes'])} node(s), {len(graph['edges'])} edge(s).",
                    "payload": {"nodes": len(graph["nodes"]), "edges": len(graph["edges"])},
                },
            )
        except Exception as exc:
            fail_agent_run(graph_run, exc)
            raise

        report_run = start_agent_run(
            matter_id,
            "report",
            "Prepare attorney-review packet",
            {"chart_rows": len(chart), "warning_count": len(warnings)},
        )
        try:
            report_html = build_report_html(matter, chart, warnings, summary)
            record_tool_call(
                report_run,
                "build_report_html",
                {"title": matter.get("title", "")},
                {"html_chars": len(report_html)},
            )
            finish_agent_run(
                report_run,
                {
                    "title": "Report prepared",
                    "body": "Review packet rendered from persisted matter state.",
                    "payload": {"html_chars": len(report_html)},
                },
            )
        except Exception as exc:
            fail_agent_run(report_run, exc)
            raise

        result = {
            "limitations": limitations,
            "chunks": chunks,
            "chart": chart,
            "warnings": warnings,
            "graph": graph,
            "summary": summary,
            "report_html": report_html,
            "audit": [],
            "redactions": redactions,
        }
        save_analysis(matter_id, result, persist_audit_events=False)
        finish_agent_run(
            controller_run,
            {
                "title": "Analysis completed",
                "body": "Pipeline completed and persisted derived evidence.",
                "payload": {
                    "limitation_count": len(limitations),
                    "chart_rows": len(chart),
                    "warning_count": len(warnings),
                },
            },
        )
    except Exception as exc:
        fail_agent_run(controller_run, exc)
        raise
    return {"result": result, "matter": get_full_matter(matter_id), "model": gemini_client().status()}


def handle_chat(matter_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    question = str(payload.get("message") or "").strip()
    if len(question) < 2:
        raise ValueError("Message is too short")
    matter = get_matter(matter_id)
    if not matter:
        raise ValueError("Matter not found")
    add_message(matter_id, "default", "user", question)
    result = answer_from_matter(question, matter, get_chart(matter_id), get_chunks(matter_id), gemini_client())
    add_message(matter_id, "default", "assistant", result["answer"])
    record_audit(
        matter_id,
        "matter_chat",
        "Matter chat answered",
        "Answered a follow-up question using current matter context.",
        {"gemini_error": result.get("gemini_error")},
    )
    return {"answer": result, "matter": get_full_matter(matter_id), "model": gemini_client().status()}


def run_evals() -> dict[str, Any]:
    cases = []

    claim = (
        "A system comprising: a sensor module configured to collect vibration data; "
        "a gateway configured to receive vibration data; a processor configured to compare vibration data "
        "against a threshold; and an alert interface configured to display an alert."
    )
    limitations = split_claim_into_limitations(claim)
    cases.append(
        {
            "name": "Claim splitting",
            "passed": len(limitations) >= 4 and limitations[0]["label"] == "1A",
            "details": f"{len(limitations)} limitations generated",
        }
    )

    redacted, redactions = redact_text("Send this to jane@example.com with token sk-live-STILTAFAKE1234567890.")
    cases.append(
        {
            "name": "Sensitive text redaction",
            "passed": "jane@example.com" not in redacted and "sk-live-STILTAFAKE1234567890" not in redacted,
            "details": ", ".join(sorted({item["type"] for item in redactions})),
        }
    )

    matter = {
        "id": 999,
        "title": "Eval Matter",
        "question": "Does the source disclose the claim?",
        "claim_text": claim,
    }
    sources = [
        {
            "id": "SRC-001",
            "title": "Sensor Reference",
            "source_type": "prior_art",
            "text": "The device collects vibration readings and sends them to a gateway.",
        },
        {
            "id": "SRC-002",
            "title": "Threshold Reference",
            "source_type": "prior_art",
            "text": "The processor compares incoming vibration readings to a configurable threshold.",
        },
    ]
    analysis = analyze_matter(matter, sources)
    chart = analysis["chart"]
    cases.append(
        {
            "name": "Every limitation represented",
            "passed": len(chart) == len(limitations),
            "details": f"{len(chart)} chart rows",
        }
    )
    cases.append(
        {
            "name": "Missing evidence visible",
            "passed": any(row["support_level"] == "Missing" for row in chart),
            "details": "Missing rows are flagged",
        }
    )
    cases.append(
        {
            "name": "Exact snippets retained",
            "passed": any("collects vibration readings" in row.get("snippet", "") for row in chart),
            "details": "Snippet text came from source",
        }
    )
    cases.append(
        {
            "name": "Legal overclaim blocked",
            "passed": any(item["warning_type"] == "legal_overclaim" for item in build_warnings([], "This definitely infringes.")),
            "details": "Unsafe legal phrase detected",
        }
    )
    cases.append(
        {
            "name": "Invented citation blocked",
            "passed": bool(validate_citations("Supported by SRC-999.", {"SRC-001"})),
            "details": "Unknown source id detected",
        }
    )
    cases.append(
        {
            "name": "Audit trail generated",
            "passed": len(analysis["audit"]) >= 5,
            "details": f"{len(analysis['audit'])} audit events",
        }
    )
    cases.append(
        {
            "name": "Report caveat present",
            "passed": "not a final legal opinion" in analysis["report_html"].lower(),
            "details": "Report includes attorney-review caveat",
        }
    )
    cases.append(
        {
            "name": "Gemini is optional",
            "passed": gemini_client().status()["required"] is False,
            "details": f"default mode: {gemini_client().status()['mode']}",
        }
    )
    return {
        "passed": sum(1 for item in cases if item["passed"]),
        "total": len(cases),
        "results": cases,
        "model": gemini_client().status(),
    }


class StiltaHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/state":
                json_response(self, build_state())
                return
            if parsed.path == "/api/evals":
                json_response(self, run_evals())
                return
            matter_id, action = parse_matter_path(parsed.path)
            if matter_id is not None:
                if action is None:
                    json_response(self, get_full_matter(matter_id))
                    return
                if action == "report":
                    report = get_latest_report(matter_id)
                    if not report:
                        json_response(self, {"error": "No report has been generated yet."}, 404)
                        return
                    body = report["html"].encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            if parsed.path == "/" or parsed.path == "/index.html":
                self.serve_static("index.html")
                return
            if parsed.path.startswith("/static/"):
                self.serve_static(parsed.path.removeprefix("/"))
                return
            json_response(self, {"error": f"Not found: GET {parsed.path}"}, 404)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/matters":
                payload = read_json(self)
                matter_id = create_matter(payload)
                record_audit(matter_id, "intake", "Matter created", "New patent matter created.", {})
                add_memory_item(
                    "matter",
                    str(matter_id),
                    "matter_profile",
                    f"{payload.get('title') or 'Untitled Patent Matter'}: {payload.get('question') or 'No review question set.'}",
                )
                json_response(self, get_full_matter(matter_id), 201)
                return
            matter_id, action = parse_matter_path(parsed.path)
            if matter_id is not None:
                payload = read_json(self)
                if action == "update":
                    update_matter(matter_id, payload)
                    clear_analysis(matter_id)
                    record_audit(matter_id, "matter", "Matter updated", "Matter fields were saved and prior analysis was cleared.", {})
                    add_memory_item(
                        "matter",
                        str(matter_id),
                        "matter_profile",
                        f"{payload.get('title') or 'Untitled Patent Matter'}: {payload.get('question') or 'No review question set.'}",
                    )
                    json_response(self, get_full_matter(matter_id))
                    return
                if action == "sources":
                    source = add_source(matter_id, payload)
                    json_response(self, {"source": source, "matter": get_full_matter(matter_id)}, 201)
                    return
                if action == "analyze":
                    json_response(self, run_analysis(matter_id), 201)
                    return
                if action == "chat":
                    json_response(self, handle_chat(matter_id, payload), 201)
                    return
            json_response(self, {"error": f"Not found: POST {parsed.path}"}, 404)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 500)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            matter_id, source_id = parse_source_delete_path(parsed.path)
            if matter_id is not None and source_id is not None:
                removed = delete_source(matter_id, source_id)
                if removed:
                    json_response(self, get_full_matter(matter_id))
                else:
                    json_response(self, {"error": "Source not found"}, 404)
                return
            matter_id, action = parse_matter_path(parsed.path)
            if matter_id is not None and action is None:
                removed = delete_matter(matter_id)
                if removed:
                    json_response(self, build_state())
                else:
                    json_response(self, {"error": "Matter not found"}, 404)
                return
            json_response(self, {"error": f"Not found: DELETE {parsed.path}"}, 404)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 500)

    def serve_static(self, filename: str) -> None:
        safe = Path(filename)
        if safe.is_absolute() or ".." in safe.parts:
            self.send_error(400)
            return
        path = PUBLIC_DIR / safe
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif path.suffix in {".html", ".css"}:
            content_type = f"{content_type}; charset=utf-8"
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    init_db()
    seed_default_matter()
    server = ThreadingHTTPServer(("127.0.0.1", 8020), StiltaHandler)
    print("Stilta Evidence Graph Lab running at http://127.0.0.1:8020")
    server.serve_forever()


if __name__ == "__main__":
    main()
