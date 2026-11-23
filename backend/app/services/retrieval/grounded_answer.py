"""Validation and rendering for source-grounded assistant responses."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app.services.retrieval.workflow_context import authentication_workflow_roles

logger = logging.getLogger(__name__)


class IncompleteGroundedAnswer(ValueError):
    """Raised when a complete, citation-valid answer was not generated."""

    def __init__(self, message: str, reasons: list[str] | None = None):
        super().__init__(message)
        self.reasons = tuple(reasons or ())


@dataclass(frozen=True)
class GroundedAnswer:
    markdown: str
    cited_source_ids: frozenset[str]


def _decode_object(raw: str) -> dict:
    text = str(raw or "").strip().lstrip("\ufeff")
    decoder = json.JSONDecoder()
    cursor = 0
    while True:
        start = text.find("{", cursor)
        if start < 0:
            raise ValueError("No JSON object in response")
        try:
            value, _ = decoder.raw_decode(text[start:])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        cursor = start + 1


def _claim_text(item) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("claim", item.get("text", ""))).strip()
    return ""


def _claim_citations(item) -> list[str]:
    raw = item.get("citations", []) if isinstance(item, dict) else []
    if isinstance(raw, str):
        raw = [raw]
    return [value for value in raw if isinstance(value, str)] if isinstance(raw, list) else []


def _normalize_compact_payload(payload: dict) -> dict:
    """Accept a compact fact list while keeping legacy response fields compatible."""
    if not isinstance(payload.get("facts"), list):
        return payload
    normalized = {**payload, "source_code_facts": [], "documentation_facts": [], "inferences": []}
    fields = {"source_code": "source_code_facts", "documentation": "documentation_facts", "inference": "inferences"}
    for item in payload["facts"]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "source_code")).lower()
        field = fields.get(kind)
        if field:
            normalized[field].append(item)
    return normalized


def _fact_items(payload: dict) -> list[tuple[str, dict]]:
    normalized = _normalize_compact_payload(payload)
    return [(field, item) for field in ("source_code_facts", "documentation_facts", "inferences")
            for item in (normalized.get(field) if isinstance(normalized.get(field), list) else [])]


def _has_uncited_claim(payload: dict, sources: list[dict]) -> bool:
    source_by_id = {str(source.get("citation_id")): source for source in sources}
    allowed = {"source_code_facts": {"source_code"}, "documentation_facts": {"documentation"},
               "inferences": {"source_code", "documentation"}}
    for field, item in _fact_items(payload):
        if not _claim_text(item):
            continue
        if not any(cid in source_by_id and source_by_id[cid].get("source_kind", "source_code") in allowed[field]
                   for cid in _claim_citations(item)):
            return True
    return False


def _strip_model_citations(text: str) -> str:
    citation = r"[\[【［]\s*S\d+(?:\s*[-‐‑‒–—]\s*S\d+|\s*,\s*S\d+)*\s*[\]】］]"
    return re.sub(rf"\s*{citation}", "", text).strip()


def render_grounded_answer(payload: dict, sources: list[dict]) -> GroundedAnswer:
    payload = _normalize_compact_payload(payload)
    source_by_id = {str(source.get("citation_id")): source for source in sources}
    used: set[str] = set()
    omitted_uncited = False
    sections: list[tuple[str, list[str]]] = []

    def validated_claims(field: str, allowed_kinds: set[str]) -> list[str]:
        nonlocal omitted_uncited
        rendered = []
        items = payload.get(field, [])
        if not isinstance(items, list):
            return rendered
        for item in items:
            text = _strip_model_citations(_claim_text(item))
            if not text:
                continue
            citations = list(dict.fromkeys(_claim_citations(item)))
            valid = [
                citation for citation in citations
                if citation in source_by_id
                and source_by_id[citation].get("source_kind", "source_code") in allowed_kinds
            ]
            if not valid:
                omitted_uncited = True
                continue
            used.update(valid)
            rendered.append(f"- {text} " + " ".join(f"[{citation}]" for citation in valid))
        return rendered

    sections.extend([
        ("Verified source-code facts", validated_claims("source_code_facts", {"source_code"})),
        ("Documentation-derived facts", validated_claims("documentation_facts", {"documentation"})),
        ("Inferences", validated_claims("inferences", {"source_code", "documentation"})),
    ])

    not_found = payload.get("not_found", [])
    missing = []
    if isinstance(not_found, list):
        missing = [f"- {_strip_model_citations(str(value))}" for value in not_found if str(value).strip()]
    if omitted_uncited:
        missing.append("- A generated claim was omitted because it lacked a valid citation to retrieved evidence.")
    if not missing:
        missing = ["- No requested detail was identified as unavailable in the retrieved evidence."]
    sections.append(("Not verified in retrieved/indexed source", missing))

    markdown = "\n\n".join(f"### {title}\n" + "\n".join(lines) for title, lines in sections if lines)
    return GroundedAnswer(markdown=markdown, cited_source_ids=frozenset(used))


def append_not_found_notes(answer: GroundedAnswer, notes: list[str]) -> GroundedAnswer:
    if not notes:
        return answer
    heading = "### Not verified in retrieved/indexed source\n"
    additions = "\n".join(f"- {note}" for note in notes)
    markdown = answer.markdown.replace(heading, heading + additions + "\n", 1)
    return GroundedAnswer(markdown=markdown, cited_source_ids=answer.cited_source_ids)


def _false_unavailable_claim(payload: dict, sources: list[dict]) -> bool:
    unavailable = re.compile(r"\b(?:not retrieved|not indexed|not included|not available in (?:the )?(?:evidence|context)|"
                             r"no evidence (?:was )?(?:retrieved|provided)|not in (?:the )?(?:retrieved )?(?:evidence|context))\b", re.I)
    notes = payload.get("not_found", [])
    if not isinstance(notes, list):
        return False
    role_aliases = {
        "backend_auth_routes": ("backend auth routes", "authentication routes", "auth route"),
        "auth_middleware": ("authentication middleware", "auth middleware", "middleware"),
        "jwt_utility": ("jwt", "token utility"),
        "auth_context": ("authcontext", "auth context", "auth provider"),
        "login_component": ("login.jsx", "login component", "sign-in component"),
        "registration_component": ("register.jsx", "registration", "register component", "sign-up component"),
        "route_guard": ("route guard", "route guards", "privateroute", "adminroute", "creatorroute"),
        "account_model": ("user model", "account model", "user.js"),
        "email_verification_page": ("verifyemail", "email verification page"),
        "email_sender": ("sendemail", "email sender", "email-sending utility"),
    }
    for note in notes:
        text = str(note).lower().replace("\\", "/")
        if not unavailable.search(text):
            continue
        for source in sources:
            path = str(source.get("file_path", "")).lower().replace("\\", "/")
            basename = path.rsplit("/", 1)[-1]
            if path and (path in text or basename in text):
                return True
            roles = authentication_workflow_roles(path)
            if any(alias in text for role in roles for alias in role_aliases.get(role, ())):
                return True
    return False


def _uncovered_required_paths(answer: GroundedAnswer, sources: list[dict], required_paths: set[str]) -> set[str]:
    cited_paths = {
        str(source.get("file_path", "")) for source in sources
        if source.get("citation_id") in answer.cited_source_ids
        and source.get("source_kind", "source_code") == "source_code"
    }
    return required_paths - cited_paths


def _uncovered_workflow_stages(payload: dict, sources: list[dict], requirements: dict[str, list[list[str]]]) -> set[str]:
    """Each stage claim must cite evidence from every available source group supplied for that stage."""
    source_by_id = {str(source.get("citation_id")): source for source in sources}
    covered: dict[str, set[str]] = {}
    for item in _normalize_compact_payload(payload).get("source_code_facts", []):
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage", "")).strip().lower()
        if not stage:
            continue
        valid = {cid for cid in _claim_citations(item) if cid in source_by_id
                 and source_by_id[cid].get("source_kind", "source_code") == "source_code"}
        covered.setdefault(stage, set()).update(valid)
    missing = set()
    for stage, source_groups in requirements.items():
        ids = covered.get(stage.lower(), set())
        if not source_groups or any(not (ids & set(group)) for group in source_groups):
            missing.add(stage)
    return missing


def generate_grounded_answer(system: str, prompt: str, sources: list[dict], complete_with_metadata,
                             required_source_paths: list[str] | None = None,
                             required_workflow_stages: dict[str, list[list[str]]] | None = None) -> GroundedAnswer:
    """Retry once with a compact response after truncation or citation/coverage validation failures."""
    retry_system = (
        system + "\n\nThis is a compact retry. Return one concise fact per requested stage and no repeated claims. "
        "Combine related evidence in one claim with multiple citations. "
        "Finish the complete JSON object. Put unsupported requested details in not_found."
    )
    required_paths = set(required_source_paths or [])
    stage_requirements = required_workflow_stages or {}
    retry_prompt = prompt
    failure_reasons: list[str] = []
    for attempt in range(2):
        response = complete_with_metadata(retry_system if attempt else system, retry_prompt)
        raw, finish_reason = response[0], response[1] if len(response) > 1 else None
        usage = response[2] if len(response) > 2 and isinstance(response[2], dict) else {}
        logger.info("grounded_answer_attempt attempt=%s max_attempts=2 finish_reason=%s prompt_tokens=%s completion_tokens=%s",
                    attempt + 1, finish_reason, usage.get("prompt_tokens"), usage.get("completion_tokens"))
        if finish_reason == "length":
            reason = "finish_reason_length"
            failure_reasons.append(reason)
            logger.warning("grounded_answer_retry attempt=%s reason=%s", attempt + 1, reason)
            retry_prompt = prompt
            continue
        try:
            payload = _decode_object(raw)
            logger.info("grounded_answer_parse attempt=%s status=success", attempt + 1)
            if payload.get("complete") is not True:
                reason = "complete_flag_false_or_missing"
                failure_reasons.append(reason)
                logger.warning("grounded_answer_retry attempt=%s reason=%s", attempt + 1, reason)
                retry_prompt = prompt
                continue
            answer = render_grounded_answer(payload, sources)
            uncovered = _uncovered_required_paths(answer, sources, required_paths)
            uncovered_stages = _uncovered_workflow_stages(payload, sources, stage_requirements)
            uncited = _has_uncited_claim(payload, sources)
            false_unavailable = _false_unavailable_claim(payload, sources)
            if uncovered or uncovered_stages or uncited or false_unavailable:
                reasons = []
                if uncovered: reasons.append("required_source_paths_missing")
                if uncovered_stages: reasons.append("required_workflow_stages_missing")
                if uncited: reasons.append("claim_without_valid_citation")
                if false_unavailable: reasons.append("supplied_source_falsely_reported_unavailable")
                failure_reasons.extend(reasons)
                logger.warning("grounded_answer_retry attempt=%s reason=%s uncovered_paths=%s uncovered_stages=%s",
                               attempt + 1, ",".join(reasons), len(uncovered), len(uncovered_stages))
                coverage = ("Required stages: " + ", ".join(sorted(uncovered_stages)) + ".") if uncovered_stages else ""
                if uncovered:
                    coverage += " Required files: " + ", ".join(sorted(uncovered)) + "."
                retry_prompt = prompt + "\n\nCoverage correction: " + coverage + " Cite the supplied evidence directly; do not claim supplied excerpts are unavailable."
                continue
            logger.info("grounded_answer_validation attempt=%s status=success cited_sources=%s required_stages=%s",
                        attempt + 1, len(answer.cited_source_ids), len(stage_requirements))
            return answer
        except (TypeError, ValueError) as exc:
            reason = "invalid_structured_output" if isinstance(exc, ValueError) else "invalid_payload_type"
            failure_reasons.append(reason)
            logger.warning("grounded_answer_parse attempt=%s status=failure reason=%s", attempt + 1, reason)
            logger.warning("grounded_answer_retry attempt=%s reason=%s", attempt + 1, reason)
            retry_prompt = prompt
            continue
    logger.error("grounded_answer_failed attempts=2 reasons=%s", ",".join(dict.fromkeys(failure_reasons)))
    raise IncompleteGroundedAnswer(
        "The model did not return a complete, citation-valid answer within the configured output limit.",
        failure_reasons,
    )
