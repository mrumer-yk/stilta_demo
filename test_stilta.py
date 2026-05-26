import unittest
import tempfile
from pathlib import Path

from gemini_client import DEFAULT_MODEL, gemini_client
from server import run_analysis, run_evals
import stilta_memory
from stilta_engine import (
    analyze_matter,
    answer_from_matter,
    build_claim_chart,
    build_warnings,
    redact_text,
    split_claim_into_limitations,
    validate_citations,
)


CLAIM = (
    "A system comprising: a sensor module configured to collect vibration data; "
    "a gateway configured to receive vibration data; "
    "a processor configured to compare the vibration data against a threshold; "
    "and an alert interface configured to display a maintenance alert."
)


class StiltaEvidenceGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        stilta_memory.DB_PATH = Path(cls._tmpdir.name) / "test_stilta.sqlite"

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_claim_splitting_creates_limitations(self):
        limitations = split_claim_into_limitations(CLAIM)
        self.assertGreaterEqual(len(limitations), 4)
        self.assertEqual(limitations[0]["label"], "1A")
        self.assertIn("vibration", limitations[0]["terms"])

    def test_redaction_removes_sensitive_values(self):
        redacted, redactions = redact_text("Email jane@example.com and key sk-live-STILTAFAKE1234567890.")
        self.assertNotIn("jane@example.com", redacted)
        self.assertNotIn("sk-live-STILTAFAKE1234567890", redacted)
        self.assertIn("email", {item["type"] for item in redactions})
        self.assertIn("api_key", {item["type"] for item in redactions})

    def test_analysis_maps_evidence_and_flags_missing(self):
        matter = {"id": 1, "title": "Eval", "question": "Review", "claim_text": CLAIM}
        sources = [
            {
                "id": "SRC-001",
                "title": "Gateway reference",
                "source_type": "prior_art",
                "text": "A gateway receives vibration readings from remote sensor modules.",
            }
        ]
        result = analyze_matter(matter, sources)
        self.assertEqual(len(result["chart"]), len(result["limitations"]))
        self.assertTrue(any(row["source_id"] == "SRC-001" for row in result["chart"]))
        self.assertTrue(any(row["support_level"] == "Missing" for row in result["chart"]))

    def test_legal_overclaim_warning(self):
        warnings = build_warnings([], "This definitely infringes and conclusively invalidates the patent.")
        self.assertGreaterEqual(len(warnings), 2)

    def test_invented_citation_warning(self):
        warnings = validate_citations("The answer cites SRC-999.", {"SRC-001"})
        self.assertEqual(warnings[0]["warning_type"], "invented_citation")

    def test_matter_scoped_citation_validation(self):
        warnings = validate_citations("Supported by M4-SRC-001.", {"M4-SRC-001"})
        self.assertEqual(warnings, [])

    def test_matter_chat_uses_current_chart(self):
        matter = {"id": 2, "title": "Eval", "question": "Review", "claim_text": CLAIM}
        sources = [
            {
                "id": "SRC-001",
                "title": "Threshold reference",
                "source_type": "prior_art",
                "text": "The processor compares vibration readings against a configurable threshold.",
            }
        ]
        result = analyze_matter(matter, sources)
        answer = answer_from_matter("Which limitation is missing support?", matter, result["chart"], result["chunks"])
        self.assertIn("Attorney review", answer["answer"])
        self.assertIn("missing", answer["answer"].lower())

    def test_gemini_default_model(self):
        self.assertEqual(gemini_client().model, DEFAULT_MODEL)

    def test_gemini_parser_and_chart_review_are_used_when_available(self):
        class FakeGemini:
            enabled = True
            model = "fake-gemini"

            def generate(self, system, prompt, max_tokens=900):
                class Result:
                    def __init__(self, text):
                        self.text = text
                        self.error = None

                if "Split this patent claim" in prompt:
                    return Result(
                        '{"limitations":[{"text":"a sensor configured to collect vibration data",'
                        '"interpretation":"AI parsed sensor limitation."},'
                        '{"text":"a processor configured to compare the vibration data against a threshold",'
                        '"interpretation":"AI parsed processor limitation."}]}'
                    )
                return Result(
                    '{"rows":[{"limitation_id":"1A","rationale":"The provided SRC-001 snippet supports the sensor collection language.",'
                    '"review_question":"Confirm whether SRC-001 is the right evidence source."}]}'
                )

        fake = FakeGemini()
        limitations = split_claim_into_limitations(CLAIM, fake)
        self.assertEqual(limitations[0]["interpretation"], "AI parsed sensor limitation.")

        chunks = [
            {
                "id": "SRC-001-C001",
                "source_id": "SRC-001",
                "chunk_index": 1,
                "text": "A sensor collects vibration readings from industrial equipment.",
                "terms": ["sensor", "collect", "vibration", "equipment"],
                "hash": "abc",
            }
        ]
        chart = build_claim_chart(limitations, chunks, fake)
        self.assertIn("Gemini-reviewed", chart[0]["ai_review"])

    def test_eval_endpoint_suite(self):
        result = run_evals()
        self.assertEqual(result["passed"], result["total"])
        self.assertGreaterEqual(result["total"], 10)

    def test_overlap_terms_present_for_matched_rows(self):
        """Chart rows with Strong/Partial support must have non-empty overlap_terms."""
        matter = {"id": 3, "title": "Overlap Test", "question": "Review", "claim_text": CLAIM}
        sources = [
            {
                "id": "SRC-001",
                "title": "Vibration gateway reference",
                "source_type": "prior_art",
                "text": (
                    "The gateway receives vibration readings from sensor modules installed on pumps. "
                    "A processor compares the vibration data against a configurable threshold "
                    "and displays a maintenance alert when the threshold is exceeded."
                ),
            }
        ]
        result = analyze_matter(matter, sources)
        matched = [row for row in result["chart"] if row["support_level"] in {"Strong", "Partial"}]
        self.assertTrue(len(matched) > 0, "Expected at least one matched row")
        for row in matched:
            self.assertTrue(
                len(row["overlap_terms"]) > 0,
                f"Row {row['limitation_id']} has support_level={row['support_level']} but no overlap_terms",
            )

    def test_delete_source_clears_analysis(self):
        """Deleting a source must remove it and wipe any saved analysis."""
        from stilta_memory import (
            add_source,
            clear_analysis,
            create_matter,
            delete_source,
            get_full_matter,
            init_db,
            save_analysis,
        )
        from stilta_engine import analyze_matter as _analyze

        init_db()
        mid = create_matter({"title": "Delete Test", "question": "Q", "claim_text": CLAIM})
        src_payload = {
            "title": "Temp source",
            "source_type": "prior_art",
            "text": "The gateway receives vibration data from sensors.",
        }
        source = add_source(mid, src_payload)
        src_id = source["id"]

        # Run analysis so chart rows exist
        matter_row = {"id": mid, "title": "Delete Test", "question": "Q", "claim_text": CLAIM}
        result = _analyze(matter_row, [source])
        save_analysis(mid, result)

        full_before = get_full_matter(mid)
        self.assertEqual(len(full_before["sources"]), 1)
        self.assertGreater(len(full_before["chart"]), 0)

        removed = delete_source(mid, src_id)
        self.assertTrue(removed)

        full_after = get_full_matter(mid)
        self.assertEqual(len(full_after["sources"]), 0, "Source should be gone")
        self.assertEqual(len(full_after["chart"]), 0, "Chart should be cleared after source deletion")

    def test_delete_matter_removes_related_records(self):
        from stilta_memory import add_source, create_matter, delete_matter, get_full_matter, get_matter, init_db

        init_db()
        mid = create_matter({"title": "Matter Delete Test", "question": "Q", "claim_text": CLAIM})
        add_source(mid, {"title": "Temp source", "source_type": "prior_art", "text": "A sensor collects vibration data."})
        run_analysis(mid)
        self.assertGreater(len(get_full_matter(mid)["audit"]), 0)

        self.assertTrue(delete_matter(mid))
        self.assertIsNone(get_matter(mid))
        with stilta_memory.connect() as conn:
            for table in [
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
                count = conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE matter_id = ?", (mid,)).fetchone()["count"]
                self.assertEqual(count, 0, f"{table} rows should be deleted")

    def test_run_analysis_records_real_stages_tool_calls_and_memory(self):
        from stilta_memory import add_source, create_matter, get_full_matter, init_db

        init_db()
        mid = create_matter({"title": "Tracked Run", "question": "Q", "claim_text": CLAIM})
        add_source(
            mid,
            {
                "title": "Tracked source",
                "source_type": "prior_art",
                "text": "A gateway receives vibration readings and a processor compares readings to a threshold.",
            },
        )
        payload = run_analysis(mid)
        full = get_full_matter(mid)
        completed_stages = {item["agent_name"] for item in full["audit"] if item["status"] == "completed"}
        self.assertIn("claim_parser", completed_stages)
        self.assertIn("matcher", completed_stages)
        self.assertTrue(any(item["tool_calls"] for item in full["audit"]), "Expected persisted tool calls")
        self.assertTrue(full["memory"], "Expected memory_items to be written")
        self.assertGreater(len(payload["matter"]["chart"]), 0)


if __name__ == "__main__":
    unittest.main()
