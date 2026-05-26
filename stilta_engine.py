from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from gemini_client import GeminiClient


STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "when",
    "where",
    "wherein",
    "configured",
    "comprising",
    "comprises",
    "comprise",
    "system",
    "method",
    "device",
    "apparatus",
    "module",
    "interface",
    "processor",
    "data",
    "information",
    "plurality",
    "least",
    "based",
    "using",
    "user",
    "said",
}

SYNONYMS = {
    "collect": {"collect", "collects", "collected", "capture", "captures", "captured", "obtain", "obtains"},
    "receive": {"receive", "receives", "receiving", "received", "accept", "ingest", "forwards", "forward"},
    "compare": {"compare", "compares", "comparing", "match", "evaluate", "evaluates", "threshold"},
    "threshold": {"threshold", "limit", "limits", "configured threshold", "configurable thresholds"},
    "alert": {"alert", "alerts", "notification", "notify", "warning", "maintenance alert"},
    "display": {"display", "displays", "dashboard", "shown", "present", "presents"},
    "sensor": {"sensor", "sensors", "sensor module", "remote sensors"},
    "gateway": {"gateway", "communication gateway", "edge gateway", "hub"},
    "vibration": {"vibration", "vibrations", "vibration readings"},
    "temperature": {"temperature", "thermal"},
    "remote": {"remote", "distributed", "field", "installed"},
    "maintenance": {"maintenance", "service", "repair", "operator"},
}

LEGAL_OVERCLAIMS = [
    "definitely infringes",
    "conclusively invalidates",
    "safe to enforce",
    "guaranteed prior art",
    "proves infringement",
    "is invalid",
    "is infringed",
]


@dataclass
class EvidenceScore:
    score: float
    support_level: str
    overlap_terms: list[str]
    rationale: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact_text(text: str) -> tuple[str, list[dict[str, str]]]:
    patterns = [
        ("api_key", r"\b(?:sk|pk|rk|ghp|AIza|xoxb|tok)[-_a-zA-Z0-9]{8,}\b"),
        ("bearer_token", r"Bearer\s+[A-Za-z0-9._\-]{12,}"),
        ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        ("phone", r"\b(?:\+?\d[\d .-]{8,}\d)\b"),
        (
            "long_secret",
            r"\b(?=[A-Za-z0-9_\-]{32,}\b)(?=[A-Za-z0-9_\-]*[A-Z])(?=[A-Za-z0-9_\-]*[a-z])(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]+\b",
        ),
        ("matter_id", r"\b(?:MATTER|CLIENT|CASE)[-_ ][A-Z0-9]{5,}\b"),
    ]
    redacted = text
    redactions: list[dict[str, str]] = []
    for label, pattern in patterns:
        for match in re.finditer(pattern, redacted):
            sample = match.group(0)
            redactions.append({"type": label, "sample": sample[:4] + "..."})
        redacted = re.sub(pattern, f"[REDACTED_{label.upper()}]", redacted)
    return redacted, redactions


def normalize_word(word: str) -> str:
    word = re.sub(r"[^a-z0-9-]", "", word.lower())
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith("ed"):
        word = word[:-2]
    elif len(word) > 4 and word.endswith("s"):
        word = word[:-1]
    return word


def expand_terms(terms: set[str]) -> set[str]:
    expanded = set(terms)
    lowered = set(terms)
    for canonical, values in SYNONYMS.items():
        normalized_values = {normalize_word(value) for value in values}
        if canonical in lowered or lowered & normalized_values:
            expanded.add(canonical)
            expanded |= normalized_values
    return expanded


def extract_terms(text: str) -> list[str]:
    lowered = text.lower()
    terms = {
        normalize_word(word)
        for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", lowered)
        if normalize_word(word) and normalize_word(word) not in STOP_WORDS
    }
    for phrase in [
        "vibration data",
        "vibration readings",
        "sensor module",
        "remote sensor",
        "communication gateway",
        "maintenance alert",
        "configurable threshold",
        "source code",
        "machine learning",
    ]:
        if phrase in lowered:
            terms.add(phrase)
    return sorted(expand_terms(terms))


def clean_limitation(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip(" .;\n\t"))
    text = re.sub(r"^(and|wherein|whereby|comprising|comprises)\s+", "", text, flags=re.I)
    return text.strip(" ,.;")


def label_for(index: int) -> str:
    return f"1{chr(ord('A') + index)}"


def extract_json_from_text(text: str) -> Any:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.I)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    match = re.search(r"(\{.*\}|\[.*\])", candidate, flags=re.S)
    if not match:
        raise ValueError("No JSON object or array found")
    return json.loads(match.group(1))


def build_limitation(label_index: int, text: str, interpretation: str | None = None) -> dict[str, Any]:
    cleaned = clean_limitation(text)
    return {
        "label": label_for(label_index),
        "text": cleaned,
        "interpretation": interpretation.strip() if interpretation else interpret_limitation(cleaned),
        "terms": extract_terms(cleaned),
    }


def local_agentic_split_claim(claim_text: str) -> list[dict[str, Any]]:
    claim = re.sub(r"\s+", " ", claim_text.strip())
    if not claim:
        return []

    body = claim
    match = re.search(r"\bcomprising\b:?", claim, flags=re.I)
    if match:
        body = claim[match.end() :].strip(" :")

    parts = [clean_limitation(part) for part in re.split(r";|\n", body)]
    parts = [part for part in parts if len(part) > 8]

    if len(parts) < 2:
        parts = [clean_limitation(part) for part in re.split(r"\bwherein\b|,\s+and\s+|,\s+a\s+", body, flags=re.I)]
        parts = [part for part in parts if len(part) > 12]

    return [build_limitation(index, part) for index, part in enumerate(parts[:12])]


def gemini_split_claim(claim_text: str, gemini: GeminiClient) -> tuple[list[dict[str, Any]], str | None]:
    prompt = (
        "Split this patent claim into element-by-element limitations. "
        "Return only JSON in this exact shape: "
        '{"limitations":[{"text":"exact or near-exact limitation text","interpretation":"plain English interpretation"}]}. '
        "Do not decide infringement, validity, patentability, or legal outcome.\n\n"
        f"Claim:\n{claim_text}"
    )
    result = gemini.generate(
        "You are a patent claim parser. Return validated JSON only.",
        prompt,
        max_tokens=900,
    )
    if not result.text:
        return [], result.error or "Gemini returned no parser text"
    try:
        parsed = extract_json_from_text(result.text)
        raw_items = parsed.get("limitations", parsed) if isinstance(parsed, dict) else parsed
        if not isinstance(raw_items, list):
            return [], "Gemini parser JSON did not contain a limitations list"
        limitations = []
        for item in raw_items[:12]:
            if isinstance(item, str):
                text = item
                interpretation = None
            elif isinstance(item, dict):
                text = str(item.get("text") or item.get("limitation") or "").strip()
                interpretation = str(item.get("interpretation") or "").strip() or None
            else:
                continue
            if len(clean_limitation(text)) < 8:
                continue
            limitations.append(build_limitation(len(limitations), text, interpretation))
        if len(limitations) < 2:
            return [], "Gemini parser produced too few usable limitations"
        return limitations, None
    except Exception as exc:
        return [], f"Gemini parser JSON failed validation: {exc}"


def split_claim_into_limitations(claim_text: str, gemini: GeminiClient | None = None) -> list[dict[str, Any]]:
    local_limitations = local_agentic_split_claim(claim_text)
    if gemini and gemini.enabled and claim_text.strip():
        limitations, error = gemini_split_claim(claim_text, gemini)
        if limitations:
            return limitations
        # Keep the local agentic parser reliable when Gemini is unavailable or malformed.
        _ = error
    return local_limitations


def interpret_limitation(text: str) -> str:
    simple = text
    simple = re.sub(r"\bconfigured to\b", "that can", simple, flags=re.I)
    simple = re.sub(r"\boperable to\b", "that can", simple, flags=re.I)
    simple = simple[:1].upper() + simple[1:] if simple else simple
    if not simple.endswith("."):
        simple += "."
    return simple


def chunk_source(source: dict[str, Any]) -> list[dict[str, Any]]:
    source_id = source["id"]
    matter_id = source.get("matter_id")
    redacted = source.get("redacted_text") or source.get("text", "")
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", redacted) if item.strip()]
    chunks: list[dict[str, Any]] = []
    for paragraph in paragraphs:
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        current = ""
        for sentence in sentences:
            if len(current) + len(sentence) > 650 and current:
                chunks.append(build_chunk(source_id, len(chunks), current, matter_id))
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            chunks.append(build_chunk(source_id, len(chunks), current, matter_id))
    if not chunks and redacted.strip():
        chunks.append(build_chunk(source_id, 0, redacted.strip(), matter_id))
    return chunks


def build_chunk(source_id: str, index: int, text: str, matter_id: int | str | None = None) -> dict[str, Any]:
    clean = re.sub(r"\s+", " ", text.strip())
    namespace = source_id
    if matter_id and not str(source_id).startswith(f"M{matter_id}-"):
        namespace = f"M{matter_id}-{source_id}"
    return {
        "id": f"{namespace}-C{index + 1:03}",
        "source_id": source_id,
        "chunk_index": index + 1,
        "text": clean,
        "terms": extract_terms(clean),
        "hash": sha256_text(clean)[:16],
    }


def score_chunk(limitation: dict[str, Any], chunk: dict[str, Any]) -> EvidenceScore:
    lim_terms = set(limitation.get("terms", []))
    chunk_terms = set(chunk.get("terms", []))
    if not lim_terms:
        return EvidenceScore(0.0, "Missing", [], "No technical terms were extracted from this limitation.")

    overlap = sorted(lim_terms & chunk_terms)
    phrase_bonus = 0.0
    lim_text = limitation["text"].lower()
    chunk_text = chunk["text"].lower()
    for term in lim_terms:
        if len(term) > 6 and term in chunk_text:
            phrase_bonus += 0.04
    action_bonus = 0.0
    for action in ["collect", "receive", "compare", "display", "alert"]:
        if action in lim_terms and action in chunk_terms:
            action_bonus += 0.08

    score = min(1.0, (len(overlap) / max(1, len(lim_terms))) + phrase_bonus + action_bonus)
    # Apply density penalty: very short chunks (< 80 chars) are unlikely to
    # provide thorough element-by-element support even with term overlap.
    if len(chunk_text) < 80:
        score = max(0.0, score - 0.08)
    if score >= 0.62:
        support = "Strong"
        rationale = "The source chunk directly overlaps key technical terms and function words."
    elif score >= 0.31:
        support = "Partial"
        rationale = "The source chunk supports some required concepts but may not cover the full limitation."
    elif score >= 0.16:
        support = "Weak"
        rationale = "The source chunk is related, but the support is too thin for confident mapping."
    else:
        support = "Missing"
        rationale = "No credible source support was found in the provided materials."
    if "threshold" in lim_text and "threshold" in chunk_text and "alert" in lim_text and "alert" not in chunk_text:
        support = "Partial"
        score = min(score, 0.5)
        rationale = "The source supports threshold comparison but not the alert/display portion."
    return EvidenceScore(round(score, 3), support, overlap[:10], rationale)


def build_claim_chart(
    limitations: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    gemini: GeminiClient | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for limitation in limitations:
        scored = [(chunk, score_chunk(limitation, chunk)) for chunk in chunks]
        scored.sort(key=lambda item: item[1].score, reverse=True)
        best_chunk, best_score = scored[0] if scored else (None, EvidenceScore(0, "Missing", [], "No sources available."))
        source_id = best_chunk["source_id"] if best_chunk and best_score.support_level != "Missing" else None
        snippet = best_chunk["text"] if best_chunk and best_score.support_level != "Missing" else ""
        rows.append(
            {
                "limitation_id": limitation["label"],
                "limitation_text": limitation["text"],
                "interpretation": limitation["interpretation"],
                "terms": limitation["terms"],
                "source_id": source_id,
                "chunk_id": best_chunk["id"] if best_chunk and source_id else None,
                "snippet": snippet,
                "score": best_score.score,
                "support_level": best_score.support_level,
                "overlap_terms": best_score.overlap_terms,
                "rationale": best_score.rationale,
                "review_question": review_question(limitation, best_score.support_level),
                "ai_review": "",
            }
        )
    return apply_gemini_chart_review(rows, gemini) if gemini and gemini.enabled else rows


def apply_gemini_chart_review(rows: list[dict[str, Any]], gemini: GeminiClient) -> list[dict[str, Any]]:
    if not rows:
        return rows
    compact_rows = [
        {
            "limitation_id": row["limitation_id"],
            "limitation_text": row["limitation_text"],
            "support_level": row["support_level"],
            "source_id": row.get("source_id"),
            "snippet": row.get("snippet", "")[:700],
            "agentic_rationale": row["rationale"],
        }
        for row in rows
    ]
    prompt = (
        "Review these local agentic claim-chart rows. Return only JSON in this exact shape: "
        '{"rows":[{"limitation_id":"1A","rationale":"short grounded rationale","review_question":"question for attorney review"}]}. '
        "Use only the provided source_id/snippet. Do not add new citations. Do not give final legal advice. "
        "Do not decide infringement, validity, or enforceability.\n\n"
        f"Rows:\n{json.dumps(compact_rows, indent=2)}"
    )
    result = gemini.generate(
        "You are a source-grounded patent evidence reviewer.",
        prompt,
        max_tokens=900,
    )
    if not result.text:
        for row in rows:
            row["ai_review"] = f"Gemini chart review unavailable: {result.error or 'no text'}"
        return rows
    try:
        parsed = extract_json_from_text(result.text)
        raw_rows = parsed.get("rows", []) if isinstance(parsed, dict) else []
        if not isinstance(raw_rows, list):
            raise ValueError("rows must be a list")
        by_id = {row["limitation_id"]: row for row in rows}
        source_ids = {row["source_id"] for row in rows if row.get("source_id")}
        for item in raw_rows:
            if not isinstance(item, dict):
                continue
            limitation_id = str(item.get("limitation_id") or "").strip()
            row = by_id.get(limitation_id)
            if not row:
                continue
            rationale = str(item.get("rationale") or "").strip()
            question = str(item.get("review_question") or "").strip()
            review_text = f"{rationale} {question}"
            if build_warnings([], review_text) or validate_citations(review_text, source_ids):
                row["ai_review"] = "Gemini chart review rejected by safety/citation audit."
                continue
            if rationale:
                row["rationale"] = rationale[:420]
            if question:
                row["review_question"] = question[:260]
            row["ai_review"] = "Gemini-reviewed rationale accepted after citation/safety audit."
    except Exception as exc:
        for row in rows:
            row["ai_review"] = f"Gemini chart review failed validation: {exc}"
    return rows


def review_question(limitation: dict[str, Any], support_level: str) -> str:
    if support_level == "Strong":
        return "Confirm whether the cited wording is enough for the intended legal theory."
    if support_level == "Partial":
        return "Review whether another source is needed for the missing part of this limitation."
    if support_level == "Weak":
        return "Check whether this is only background context rather than element-by-element support."
    return "Upload or search for additional evidence for this limitation."


def build_warnings(chart: list[dict[str, Any]], text_to_audit: str = "") -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for row in chart:
        if row["support_level"] == "Missing":
            warnings.append(
                {
                    "severity": "high",
                    "warning_type": "missing_evidence",
                    "message": f"{row['limitation_id']} has no cited support in the current source library.",
                    "payload": {"limitation_id": row["limitation_id"]},
                }
            )
        elif row["support_level"] in {"Weak", "Partial"}:
            warnings.append(
                {
                    "severity": "medium",
                    "warning_type": "thin_support",
                    "message": f"{row['limitation_id']} is only {row['support_level'].lower()}ly supported.",
                    "payload": {"limitation_id": row["limitation_id"], "support_level": row["support_level"]},
                }
            )
    lowered = text_to_audit.lower()
    for phrase in LEGAL_OVERCLAIMS:
        if phrase in lowered:
            warnings.append(
                {
                    "severity": "high",
                    "warning_type": "legal_overclaim",
                    "message": f"Unsafe legal conclusion detected: '{phrase}'. Use attorney-review language.",
                    "payload": {"phrase": phrase},
                }
            )
    return warnings


def build_graph(matter_id: int, sources: list[dict[str, Any]], chart: list[dict[str, Any]]) -> dict[str, Any]:
    nodes = [{"id": f"M-{matter_id}", "type": "matter", "label": "Matter"}]
    edges = []
    source_ids = set()
    for row in chart:
        limitation_node = f"L-{row['limitation_id']}"
        nodes.append({"id": limitation_node, "type": "limitation", "label": row["limitation_id"]})
        edges.append({"from": f"M-{matter_id}", "to": limitation_node, "type": "contains"})
        if row.get("source_id"):
            source_id = row["source_id"]
            if source_id not in source_ids:
                source = next((item for item in sources if item["id"] == source_id), None)
                nodes.append({"id": source_id, "type": "source", "label": source["title"] if source else source_id})
                source_ids.add(source_id)
            snippet_node = f"SNP-{row['limitation_id']}"
            nodes.append({"id": snippet_node, "type": "snippet", "label": row["support_level"]})
            edges.append({"from": limitation_node, "to": snippet_node, "type": row["support_level"].lower()})
            edges.append({"from": snippet_node, "to": source_id, "type": "cites"})
        else:
            warning_node = f"WARN-{row['limitation_id']}"
            nodes.append({"id": warning_node, "type": "warning", "label": "Missing evidence"})
            edges.append({"from": limitation_node, "to": warning_node, "type": "missing_support"})
    return {"nodes": nodes, "edges": edges}


def draft_grounded_summary(chart: list[dict[str, Any]], gemini_text: str | None = None) -> str:
    strong = [row for row in chart if row["support_level"] == "Strong"]
    partial = [row for row in chart if row["support_level"] in {"Partial", "Weak"}]
    missing = [row for row in chart if row["support_level"] == "Missing"]
    parts = []
    if strong:
        ids = ", ".join(f"{row['limitation_id']} ({row['source_id']})" for row in strong)
        parts.append(f"The current source library appears to strongly support {ids}.")
    if partial:
        ids = ", ".join(row["limitation_id"] for row in partial)
        parts.append(f"{ids} need attorney review because the current support is partial or weak.")
    if missing:
        ids = ", ".join(row["limitation_id"] for row in missing)
        parts.append(f"{ids} are not supported by the provided sources.")
    if not parts:
        parts.append("No claim-chart support has been generated yet.")
    parts.append("This is an analysis aid for attorney review, not a final legal opinion.")
    return " ".join(parts)


def validate_citations(text: str, source_ids: set[str]) -> list[dict[str, Any]]:
    warnings = []
    cited = set(re.findall(r"\b(?:M\d+-)?SRC-\d{3}\b", text))
    for source_id in cited:
        if source_id not in source_ids:
            warnings.append(
                {
                    "severity": "high",
                    "warning_type": "invented_citation",
                    "message": f"Referenced source id {source_id} does not exist in this matter.",
                    "payload": {"source_id": source_id},
                }
            )
    return warnings


def build_report_html(matter: dict[str, Any], chart: list[dict[str, Any]], warnings: list[dict[str, Any]], summary: str) -> str:
    rows = []
    for row in chart:
        snippet = html.escape(row.get("snippet") or "No cited snippet.")
        source = html.escape(row.get("source_id") or "Missing")
        rows.append(
            "<tr>"
            f"<td>{html.escape(row['limitation_id'])}</td>"
            f"<td>{html.escape(row['limitation_text'])}</td>"
            f"<td><strong>{html.escape(row['support_level'])}</strong><br>{html.escape(row['rationale'])}</td>"
            f"<td>{source}<br><span>{snippet}</span></td>"
            f"<td>{html.escape(row['review_question'])}</td>"
            "</tr>"
        )
    warning_items = "".join(f"<li>{html.escape(item['message'])}</li>" for item in warnings) or "<li>No warnings.</li>"
    return f"""
<article class="report">
  <h1>{html.escape(matter.get("title") or "Patent Matter")}</h1>
  <p><strong>Question:</strong> {html.escape(matter.get("question") or "Not specified")}</p>
  <p><strong>Grounded summary:</strong> {html.escape(summary)}</p>
  <h2>Claim Chart</h2>
  <table>
    <thead><tr><th>Limitation</th><th>Text</th><th>Support</th><th>Evidence</th><th>Review</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <h2>Warnings</h2>
  <ul>{warning_items}</ul>
  <p class="caveat">Attorney review required. This report is not a final legal opinion.</p>
</article>
""".strip()


def answer_from_matter(question: str, matter: dict[str, Any], chart: list[dict[str, Any]], chunks: list[dict[str, Any]], gemini: GeminiClient | None = None) -> dict[str, Any]:
    redacted_question, redactions = redact_text(question)
    terms = set(extract_terms(redacted_question))
    relevant_rows = [
        row
        for row in chart
        if terms & set(row.get("terms", [])) or row["limitation_id"].lower() in redacted_question.lower()
    ]
    if not relevant_rows and any(word in redacted_question.lower() for word in ["missing", "unsupported", "weak"]):
        relevant_rows = [row for row in chart if row["support_level"] in {"Missing", "Weak", "Partial"}]
    if not relevant_rows:
        relevant_rows = chart[:3]

    local_answer = build_agentic_chat_answer(redacted_question, relevant_rows)
    gemini_error = None
    answer = local_answer
    if gemini and gemini.enabled:
        context_lines = []
        for row in relevant_rows[:6]:
            context_lines.append(
                json.dumps(
                    {
                        "limitation": row["limitation_id"],
                        "support": row["support_level"],
                        "source_id": row.get("source_id"),
                        "snippet": row.get("snippet", "")[:700],
                        "rationale": row.get("rationale"),
                    }
                )
            )
        prompt = (
            f"Matter title: {matter.get('title')}\n"
            f"Question: {redacted_question}\n\n"
            "Current claim-chart evidence rows:\n"
            + "\n".join(context_lines)
            + "\n\nAnswer using only these rows. Cite source ids when discussing evidence. "
            "If evidence is missing, say so clearly. Do not provide final legal advice."
        )
        result = gemini.generate(
            "You are a patent analysis assistant. You must stay grounded in cited evidence.",
            prompt,
            max_tokens=650,
        )
        if result.text:
            valid_source_ids = {row["source_id"] for row in chart if row.get("source_id")}
            citation_warnings = validate_citations(result.text, valid_source_ids)
            cited_source_ids = set(re.findall(r"\bSRC-\d{3}\b", result.text))
            if valid_source_ids and not cited_source_ids:
                citation_warnings.append(
                    {
                        "severity": "high",
                        "warning_type": "missing_citation",
                        "message": "Gemini discussed evidence without citing a source id.",
                        "payload": {},
                    }
                )
            safety_warnings = build_warnings([], result.text)
            looks_complete = result.text.rstrip().endswith((".", "?", "!", ")"))
            if not citation_warnings and not safety_warnings and looks_complete:
                answer = result.text
            else:
                gemini_error = "Gemini answer failed citation or safety audit; local agentic answer used."
        else:
            gemini_error = result.error
    return {
        "answer": answer,
        "redactions": redactions,
        "gemini_error": gemini_error,
    }


def build_agentic_chat_answer(question: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No claim-chart analysis exists yet. Add sources and run analysis first."
    lines = []
    for row in rows:
        if row.get("source_id"):
            lines.append(
                f"{row['limitation_id']} is {row['support_level'].lower()}ly supported by {row['source_id']}: {row['rationale']}"
            )
        else:
            lines.append(f"{row['limitation_id']} is missing support in the current source library.")
    return " ".join(lines) + " Attorney review is required before relying on this analysis."


def analyze_matter(matter: dict[str, Any], sources: list[dict[str, Any]], gemini: GeminiClient | None = None) -> dict[str, Any]:
    limitations = split_claim_into_limitations(matter.get("claim_text", ""), gemini)
    chunks = []
    redactions = []
    safe_sources = []
    for source in sources:
        redacted, found = redact_text(source.get("text", ""))
        safe_source = {**source, "redacted_text": redacted}
        safe_sources.append(safe_source)
        redactions.extend(found)
        chunks.extend(chunk_source(safe_source))
    chart = build_claim_chart(limitations, chunks, gemini)
    summary = draft_grounded_summary(chart)
    warnings = build_warnings(chart, summary)
    warnings.extend(validate_citations(summary, {source["id"] for source in sources}))
    if redactions:
        warnings.append(
            {
                "severity": "medium",
                "warning_type": "redaction",
                "message": f"{len(redactions)} sensitive item(s) redacted before analysis output.",
                "payload": {"count": len(redactions), "types": sorted({item['type'] for item in redactions})},
            }
        )
    graph = build_graph(int(matter["id"]), sources, chart)
    report_html = build_report_html(matter, chart, warnings, summary)
    audit = [
        {"event_type": "intake", "title": "Matter loaded", "body": f"Analyzing {len(sources)} source(s)."},
        {"event_type": "claim_parser", "title": "Claim parsed", "body": f"{len(limitations)} limitation(s) generated."},
        {"event_type": "corpus", "title": "Sources chunked", "body": f"{len(chunks)} source chunk(s) created."},
        {"event_type": "matcher", "title": "Evidence matched", "body": f"{len(chart)} claim chart row(s) scored."},
        {"event_type": "auditor", "title": "Citation audit", "body": f"{len(warnings)} warning(s) generated."},
        {"event_type": "report", "title": "Report prepared", "body": "Review packet rendered from persisted matter state."},
    ]
    return {
        "limitations": limitations,
        "chunks": chunks,
        "chart": chart,
        "warnings": warnings,
        "graph": graph,
        "summary": summary,
        "report_html": report_html,
        "audit": audit,
        "redactions": redactions,
    }
