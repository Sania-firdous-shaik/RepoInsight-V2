"""Citation-preserving context assembly with a deterministic token budget."""
from __future__ import annotations
import hashlib
from pathlib import PurePosixPath
from types import SimpleNamespace
import re

from app.services.retrieval.workflow_context import authentication_workflow_roles
from app.services.retrieval.query_analysis import QueryProfile, code_tokens

def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)

def source_kind_for_path(path: str) -> str:
    return "documentation" if PurePosixPath(str(path)).suffix.lower() in {".md", ".rst", ".txt"} else "source_code"


_AUTH_WINDOW_TERMS = {
    "backend_auth_routes": ("register", "signup", "verify", "verification", "activate", "isactive", "isverified", "login", "compare", "bcrypt", "jwt", "token"),
    "auth_middleware": ("jwt", "token", "verify", "req.user", "user"),
    "jwt_utility": ("jwt", "sign", "verify", "token", "expiresin"),
    "auth_context": ("login", "register", "logout", "token", "user", "auth", "verify"),
    "login_component": ("login", "password", "submit", "token", "auth"),
    "registration_component": ("register", "signup", "verification", "email", "password", "token"),
    "route_guard": ("admin", "creator", "role", "auth", "navigate", "redirect", "user", "outlet"),
    "account_model": ("active", "verified", "password", "role", "token", "email"),
    "email_verification_page": ("verify", "verification", "activate", "token", "email", "success"),
    "email_sender": ("email", "verification", "token", "sendmail", "transporter"),
}


def _auth_window_score(hit) -> int:
    """Prefer line windows that contain implementation details for their workflow role."""
    content = str(hit.payload.get("content", "")).lower()
    roles = authentication_workflow_roles(str(hit.payload.get("file_path", "")))
    terms = {term for role in roles for term in _AUTH_WINDOW_TERMS.get(role, ())}
    return sum(1 for term in terms if re.search(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])", content))


def _query_window_score(hit, profile: QueryProfile | None) -> float:
    if profile is None:
        return 0.0
    payload = hit.payload
    content_tokens = set(code_tokens(str(payload.get("content", ""))))
    source_terms = set(profile.terms)
    expanded_terms = set(profile.expanded_terms)
    score = 2.0 * len(source_terms & content_tokens) + 0.35 * len(expanded_terms & content_tokens)
    symbol = str(payload.get("symbol_name", "")).lower()
    path = str(payload.get("file_path", "")).lower().replace("\\", "/")
    if any(identifier.lower() == symbol for identifier in profile.identifiers):
        score += 20.0
    if any(path == requested.lower() or path.endswith("/" + requested.lower()) for requested in profile.file_paths):
        score += 40.0
    if any(endpoint.lower() in str(payload.get("content", "")).lower() for endpoint in profile.endpoints):
        score += 20.0
    return score


def split_hits_for_budget(hits, token_budget: int, max_lines_per_source: int | None = None):
    """Split chunks on line/character boundaries, preserving exact citation offsets."""
    max_chars = max(256, (token_budget - 80) * 4)
    result = []
    for hit in hits:
        payload = hit.payload
        content = str(payload.get("content", ""))
        source_lines = content.splitlines(keepends=True) or [content]
        needs_line_split = max_lines_per_source is not None and len(source_lines) > max_lines_per_source
        if estimate_tokens(content) <= token_budget - 80 and not needs_line_split:
            result.append(hit)
            continue

        line_windows: list[tuple[str, int]] = []
        window_size = max_lines_per_source or len(source_lines)
        char_offset = 0
        for line_offset in range(0, len(source_lines), window_size):
            lines = source_lines[line_offset:line_offset + window_size]
            line_windows.append(("".join(lines), char_offset))
            char_offset += sum(map(len, lines))

        pieces: list[tuple[str, int]] = []
        for window, window_offset in line_windows:
            if len(window) <= max_chars:
                pieces.append((window, window_offset))
                continue
            current, current_offset = "", window_offset
            consumed = 0
            for line in window.splitlines(keepends=True) or [window]:
                line_parts = [line[i:i + max_chars] for i in range(0, len(line), max_chars)] or [line]
                for part in line_parts:
                    if current and len(current) + len(part) > max_chars:
                        pieces.append((current, current_offset))
                        consumed += len(current)
                        current, current_offset = "", window_offset + consumed
                    current += part
            if current:
                pieces.append((current, current_offset))

        for index, (piece, char_offset) in enumerate(pieces):
            part_payload = dict(payload)
            prefix = content[:char_offset]
            start = int(payload.get("start_line", 1)) + prefix.count("\n")
            piece_line_count = max(1, len(piece.splitlines()))
            end = min(int(payload.get("end_line", start)), start + piece_line_count - 1)
            part_payload.update({"content": piece, "start_line": start, "end_line": end,
                                 "content_hash": hashlib.sha256(piece.encode()).hexdigest()})
            point = SimpleNamespace(id=f"{hit.id}:part:{index}", payload=part_payload)
            result.append(type(hit)(point=point, score=hit.score, rank=hit.rank))
    return result

def build_context(hits, token_budget: int = 8000, source_offset: int = 0,
                  max_lines_per_source: int | None = None, fair_by_file: bool = False,
                  prioritize_auth_workflow: bool = False, profile: QueryProfile | None = None):
    if max_lines_per_source is not None:
        hits = split_hits_for_budget(hits, token_budget, max_lines_per_source)
    if fair_by_file:
        grouped: dict[str, list] = {}
        for hit in hits:
            grouped.setdefault(str(hit.payload.get("file_path", "unknown")), []).append(hit)
        if prioritize_auth_workflow or profile is not None:
            for path, file_hits in grouped.items():
                file_hits.sort(key=lambda hit: (
                    -(_query_window_score(hit, profile) + (_auth_window_score(hit) if prioritize_auth_workflow else 0)),
                    int(getattr(hit, "rank", 0)),
                    int(hit.payload.get("start_line", 1)),
                ))
        interleaved = []
        while grouped:
            for path in list(grouped):
                interleaved.append(grouped[path].pop(0))
                if not grouped[path]:
                    del grouped[path]
        hits = interleaved
    context_parts, sources, used = [], [], 0
    for hit in hits:
        payload = hit.payload
        path = str(payload.get("file_path", "unknown"))
        start = int(payload.get("start_line", 1))
        end = int(payload.get("end_line", start))
        content = str(payload.get("content", ""))
        source_no = source_offset + len(sources) + 1
        source_kind = source_kind_for_path(path)
        header = f"[S{source_no}] {path}:{start}-{end} ({source_kind}; {payload.get('symbol_type', 'section')} {payload.get('symbol_name', '')})"
        remaining = token_budget - used - estimate_tokens(header) - 8
        if remaining <= 0:
            break
        max_chars = remaining * 4
        selected_content = content
        if len(selected_content) > max_chars:
            selected_content = selected_content[:max_chars].rsplit("\n", 1)[0]
            if not selected_content.strip():
                continue
            end = min(end, start + max(0, len(selected_content.splitlines()) - 1))
            header = f"[S{source_no}] {path}:{start}-{end} ({source_kind}; {payload.get('symbol_type', 'section')} {payload.get('symbol_name', '')})"
        block = f"{header}\n```\n{selected_content}\n```"
        block_tokens = estimate_tokens(block)
        if used + block_tokens > token_budget:
            continue
        context_parts.append(block)
        used += block_tokens
        sources.append({"citation_id": f"S{source_no}", "file_path": path, "start_line": start, "end_line": end,
                        "symbol_name": payload.get("symbol_name", ""),
                        "symbol_type": payload.get("symbol_type", "section"),
                        "source_kind": source_kind,
                        "language": payload.get("language", ""),
                        "chunk_id": payload.get("chunk_id", str(hit.id)),
                        "relevance": round(float(hit.score), 6)})
    return {"context": "\n\n".join(context_parts), "sources": sources, "estimated_tokens": used}
