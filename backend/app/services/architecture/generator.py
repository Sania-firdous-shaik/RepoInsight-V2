"""Bounded Groq interpretation of indexed architecture evidence."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

from fastapi import HTTPException

from app.core.config import settings
from app.services.ai.groq_service import GroqRateLimitError, complete_with_metadata
from app.services.architecture.graph import _citation, _indexed_line, _source_files, validate_architecture_graph

logger = logging.getLogger(__name__)
MAX_CONTEXT_CHARS = 24000
MAX_EVIDENCE_FILES = 20
MAX_EVIDENCE_REFS = 32
ARCHITECTURE_TYPES = {"layer", "component", "route", "service", "artifact", "database", "external", "client"}
_CONTEXT_CUE = re.compile(
    r"flask|fastapi|express|@app\.route|@router\.|router\.(?:get|post|put|patch|delete)|"
    r"fetch\s*\(|axios\.|request\.json|response\.json|jsonify|render_template|"
    r"joblib\.load|pickle\.load|\.transform\s*\(|\.predict(?:_proba)?\s*\(|"
    r"haversine|calculate_distance|threshold|create_engine|mongoose\.connect|"
    r"connect\(|sendmail|send_mail|sendmail|sendMail|payment|orders\.create|"
    r"read_csv|read_parquet|train_test_split|fit\s*\(|save_model|to_csv|training|dataset|"
    r"require\(['\"]|from\s+\S+\s+import|^\s*import\s+",
    re.I,
)
_SECRET_VALUE = re.compile(
    r"(?i)((?:api[_-]?key|secret|password|token|private[_-]?key|credential)\s*[:=]\s*)"
    r"(['\"])([^'\"]+)(\2)"
)
_GENERIC_NAME_WORDS = {"application", "api", "backend", "frontend", "component", "service", "data", "model",
                       "feature", "engineering", "preprocessing", "inference", "risk", "evaluation", "user",
                       "client", "browser", "ui", "module", "modules", "process", "pipeline", "artifact"}


def _source_evidence(points: Iterable[Any], *, limit_chars: int = MAX_CONTEXT_CHARS) -> tuple[str, dict[str, dict]]:
    """Select relevant indexed chunks as compact, line-addressable evidence items."""
    files = _source_files(points)
    ranked_files: list[tuple[int, str, dict]] = []
    for path, data in files.items():
        lines = sorted(data.get("lines", {}).items())
        if not lines:
            continue
        content = "\n".join(line for _, line in lines)
        lower = path.lower()
        # Use repository-independent architectural roles to approximate V1's
        # broad FILE/CODE context, while keeping the evidence set bounded.
        score = sum((3 if cue in lower else 0) for cue in (
            "app.py", "main.py", "server", "index.", "route", "controller", "handler",
            "template", "frontend", "client", "model", "service", "config", "database",
            "preprocess", "feature", "predict", "train", "inference", "view", "page",
            "notebook", "dataset", "data/", "requirements.txt", "pyproject", ".env.example",
        ))
        score += min(12, len(_CONTEXT_CUE.findall(content)))
        if re.search(r"read_csv|read_parquet|train_test_split|\.fit\s*\(|save_model|training|dataset", content, re.I):
            score += 6
        if any(word in content.lower() for word in ("predict_proba", "joblib.load", "threshold")):
            score += 10
        if any(word in content.lower() for word in ("fetch(", "axios.", "formdata", "addEventListener")):
            score += 7
        if score:
            ranked_files.append((score, path, data))
    ranked_files.sort(key=lambda item: (-item[0], item[1].lower()))

    evidence: dict[str, dict] = {}
    blocks: list[str] = []
    remaining = limit_chars
    selected_files = {path for _, path, _data in ranked_files[:MAX_EVIDENCE_FILES]}
    ranked_chunks: list[tuple[int, str, dict, dict]] = []
    for _file_score, path, data in ranked_files:
        if path not in selected_files:
            continue
        for chunk in data.get("chunks", []):
            chunk_text = str(chunk.get("content", ""))
            cue_count = len(_CONTEXT_CUE.findall(chunk_text))
            path_role = bool(re.search(
                r"(?:^|/)(?:app|main|index|server|api|routes?|controllers?|handlers?|services?|"
                r"models?|features?|preprocess(?:or|ing)?|inference|predict(?:ion)?|train(?:ing)?|"
                r"templates?|views?|pages?|components?|notebooks?|requirements\.txt|pyproject\.toml|"
                r"\.env\.example|dockerfile|makefile)(?:\.|/|$)", path, re.I
            ))
            if cue_count or path_role:
                score = cue_count + (2 if path_role else 0) + (8 if re.search(r"predict_proba|joblib\.load|threshold|/predict", chunk_text, re.I) else 0)
                ranked_chunks.append((score, path, data, chunk))
    ranked_chunks.sort(key=lambda item: (-item[0], item[1].lower(), _indexed_line(item[3], item[1], "start_line", 1)))
    # V1 gives the model repository-wide file context. Preserve that breadth
    # under V2's token budget by allocating one excerpt per file before any
    # file receives a second excerpt, instead of letting high-cue files crowd
    # out other parts of the runtime workflow.
    chunks_by_path_candidates: dict[str, list[tuple[int, str, dict, dict]]] = {}
    for item in ranked_chunks:
        chunks_by_path_candidates.setdefault(item[1], []).append(item)
    balanced_chunks: list[tuple[int, str, dict, dict]] = []
    for round_index in range(max((len(rows) for rows in chunks_by_path_candidates.values()), default=0)):
        for path in sorted(chunks_by_path_candidates, key=lambda value: value.lower()):
            rows = chunks_by_path_candidates[path]
            if round_index < len(rows):
                balanced_chunks.append(rows[round_index])
    chunks_by_path: dict[str, int] = {}
    for _score, path, data, chunk in balanced_chunks:
        if len(evidence) >= MAX_EVIDENCE_REFS or chunks_by_path.get(path, 0) >= 4:
            continue
        start = _indexed_line(chunk, path, "start_line", 1)
        end = _indexed_line(chunk, path, "end_line", start)
        lines = str(chunk.get("content", "")).splitlines()
        selected: set[int] = set()
        if len(lines) <= 90:
            selected.update(range(len(lines)))
        else:
            for index, line in enumerate(lines):
                if index < 8 or _CONTEXT_CUE.search(line):
                    selected.update(range(max(0, index - 2), min(len(lines), index + 3)))
        line_map = chunk.get("source_line_map")
        snippet = "\n".join(
            f"{line_map[index] if isinstance(line_map, list) and index < len(line_map) else start + index}: {lines[index]}"
            for index in sorted(selected)
        )
        snippet = _SECRET_VALUE.sub(r"\1\2[REDACTED]\4", snippet)
        if len(snippet) > 2200:
            snippet = snippet[:2200].rsplit("\n", 1)[0]
        if not snippet or len(snippet) + len(path) + 48 > remaining:
            continue
        eid = f"E{len(evidence) + 1}"
        citation = {"file_path": path, "start_line": start, "end_line": end}
        evidence[eid] = {"file_path": path, "start_line": start, "end_line": end,
                         "citation": citation, "text": snippet}
        blocks.append(f"[{eid}] FILE: {path} (lines {start}-{end})\nCODE:\n{snippet}")
        remaining -= len(snippet) + len(path) + 48
        chunks_by_path[path] = chunks_by_path.get(path, 0) + 1
        if remaining < 400:
            break
    return "\n\n".join(blocks), evidence


def _readme_context(points: Iterable[Any], *, limit_chars: int = 2400) -> str:
    """Add a small, clearly non-authoritative README excerpt as architecture context."""
    files: dict[str, list[tuple[int, str]]] = {}
    for point in points:
        payload = point.payload if hasattr(point, "payload") else point.get("payload", {})
        path = str(payload.get("file_path", "")).replace("\\", "/")
        if path.rsplit("/", 1)[-1].lower() not in {"readme.md", "readme.rst", "readme.txt"}:
            continue
        files.setdefault(path, []).append((_indexed_line(payload, path, "start_line", 1), str(payload.get("content", ""))))
    blocks = []
    remaining = limit_chars
    for path in sorted(files, key=str.lower):
        chunks = sorted(files[path], key=lambda item: item[0])
        text = "\n".join(content for _start, content in chunks)
        text = _SECRET_VALUE.sub(r"\1\2[REDACTED]\4", text)
        excerpt = text[:remaining].rsplit("\n", 1)[0]
        if not excerpt:
            break
        start = chunks[0][0]
        end = chunks[-1][0] + max(0, len(chunks[-1][1].splitlines()) - 1)
        block = f"[README supporting context only; not implementation evidence] {path}:{start}-{end}\n{excerpt}"
        if len(block) > remaining:
            block = block[:remaining]
        blocks.append(block)
        remaining -= len(block)
        if remaining < 200:
            break
    return "\n\n".join(blocks)


def _json_object(raw: str) -> dict:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("response_not_object")
    return value


def _refs(row: dict, evidence: dict[str, dict]) -> list[dict]:
    ids = row.get("source_refs")
    if not isinstance(ids, list) or not ids or any(ref not in evidence for ref in ids):
        return []
    citations = {tuple((evidence[ref]["citation"][key] for key in ("file_path", "start_line", "end_line"))):
                 evidence[ref]["citation"] for ref in ids}
    return list(citations.values())


def _name_anchored(name: str, refs: list[dict], evidence: dict[str, dict]) -> bool:
    cited = [item for item in evidence.values() if item["citation"] in refs]
    text = " ".join(item["file_path"] + " " + item["text"] for item in cited).lower()
    terms = [term for term in re.findall(r"[a-z0-9_]+", name.lower())
             if len(term) > 2 and term not in _GENERIC_NAME_WORDS]
    if not terms:
        return False
    return any(term in text for term in terms)


def _relation_anchored(source: dict, target: dict, refs: list[dict], evidence: dict[str, dict]) -> bool:
    cited = [item for item in evidence.values() if item["citation"] in refs]
    text = " ".join(item["file_path"] + " " + item["text"] for item in cited).lower()
    def anchors(component):
        return [term for term in re.findall(r"[a-z0-9_]+", component.get("name", "").lower())
                if len(term) > 2 and term not in _GENERIC_NAME_WORDS and term in text]
    source_terms, target_terms = anchors(source), anchors(target)
    return bool(source_terms and target_terms)


def _merge_interpretation(graph: dict, response: dict, evidence: dict[str, dict]) -> dict:
    components = response.get("components")
    relationships = response.get("relationships")
    if not isinstance(components, list) or not isinstance(relationships, list):
        raise ValueError("invalid_components_or_relationships")
    nodes = {node["id"]: node for node in graph["nodes"]}
    name_to_id = {node["label"].strip().lower(): node["id"] for node in graph["nodes"]}
    ai_components: dict[str, dict] = {}
    rejected_components = 0
    rejection_reasons: dict[str, int] = {}
    def reject(reason: str) -> None:
        rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

    for row in components[:32]:
        if not isinstance(row, dict):
            rejected_components += 1
            reject("component_invalid_shape")
            continue
        name = str(row.get("name", "")).strip()[:120]
        requested_id = str(row.get("id", "")).strip()
        component_type = str(row.get("type", "")).lower()
        citations = _refs(row, evidence)
        if not name or component_type not in ARCHITECTURE_TYPES or not citations:
            rejected_components += 1
            reject("component_missing_name_type_or_source_reference")
            continue
        if component_type == "external" and set(re.findall(r"[a-z0-9_-]+", name.lower())) & {
                "joblib", "numpy", "pandas", "sklearn", "scikit-learn", "xgboost", "scipy", "torch", "tensorflow"}:
            rejected_components += 1
            reject("ordinary_library_misclassified_as_external_service")
            continue
        existing_id = requested_id if requested_id in nodes else name_to_id.get(name.lower())
        if existing_id:
            node = nodes[existing_id]
            node["citations"] = list({(c["file_path"], c["start_line"], c["end_line"]): c
                                      for c in [*node.get("citations", []), *citations]}.values())
            responsibility = str(row.get("responsibility", "")).strip()
            if responsibility and len(responsibility) <= 500:
                node["responsibility"] = responsibility
            ai_components[requested_id or existing_id] = {**row, "id": existing_id, "citations": citations}
            continue
        if not requested_id.startswith("ai:") or not re.fullmatch(r"[A-Za-z0-9:_./-]{1,120}", requested_id) or requested_id in nodes:
            rejected_components += 1
            reject("component_id_invalid")
            continue
        if not _name_anchored(name, citations, evidence):
            rejected_components += 1
            reject("component_name_not_anchored_in_cited_source")
            continue
        node = {"id": requested_id, "label": name, "type": component_type, "parent_id": None, "path": None,
                "architecture_component": True, "responsibility": str(row.get("responsibility", ""))[:500],
                "technologies": [str(value)[:80] for value in row.get("technologies", [])[:8]
                                 if isinstance(value, str)],
                "symbols": [], "routes": [], "citations": citations, "expandable": False}
        if component_type == "artifact":
            artifact = str(row.get("artifact_path", ""))
            if artifact and any(artifact in evidence[ref]["text"] for ref in row.get("source_refs", []) if ref in evidence):
                node["artifact_path"] = artifact
        nodes[requested_id] = node
        ai_components[requested_id] = {**row, "id": requested_id, "citations": citations}

    existing_edges = {(edge["source"], edge["target"], edge["type"]): edge for edge in graph["edges"]}
    graph_nodes = {node["id"]: node for node in graph["nodes"]}
    rejected_relationships = 0
    for row in relationships[:64]:
        if not isinstance(row, dict):
            rejected_relationships += 1
            reject("relationship_invalid_shape")
            continue
        source_id, target_id = str(row.get("source_id", "")), str(row.get("target_id", ""))
        # The prompt asks Groq to return only additions. A valid new edge may
        # therefore connect two deterministic nodes without repeating those
        # nodes in the response. Resolve AI ids first, then existing graph ids.
        source = ai_components.get(source_id) or graph_nodes.get(source_id)
        target = ai_components.get(target_id) or graph_nodes.get(target_id)
        if not source or not target:
            rejected_relationships += 1
            reject("relationship_endpoint_not_in_validated_graph")
            continue
        source = {**source, "name": source.get("name") or source.get("label", "")}
        target = {**target, "name": target.get("name") or target.get("label", "")}
        citations = _refs(row, evidence)
        if not citations:
            rejected_relationships += 1
            reject("relationship_missing_source_reference")
            continue
        relation = str(row.get("type", "uses_service"))
        if relation not in {"imports", "api_call", "uses_service", "uses_model", "connects_to", "calls_external", "renders_in"}:
            rejected_relationships += 1
            reject("relationship_type_unsupported")
            continue
        key = (source["id"], target["id"], relation)
        if key in existing_edges:
            continue
        source_paths = {citation["file_path"] for citation in source.get("citations", [])}
        target_paths = {citation["file_path"] for citation in target.get("citations", [])}
        deterministic_path_relation = any(
            edge["type"] == relation and graph_nodes.get(edge["source"], {}).get("path") in source_paths
            and graph_nodes.get(edge["target"], {}).get("path") in target_paths
            for edge in graph["edges"]
        )
        if not deterministic_path_relation and not _relation_anchored(source, target, citations, evidence):
            rejected_relationships += 1
            reject("relationship_not_anchored_in_cited_source")
            continue
        edge = {"id": f"edge:ai:{len(graph['edges']) + 1}", "source": source["id"], "target": target["id"],
                "type": relation, "label": str(row.get("label", ""))[:100] or None, "verified": True,
                "citations": citations}
        graph["edges"].append(edge)
        existing_edges[key] = edge

    summary = str(response.get("summary", "")).strip()
    summary_refs = response.get("summary_refs", [])
    if summary and len(summary) <= 1000 and isinstance(summary_refs, list) and summary_refs and all(ref in evidence for ref in summary_refs):
        graph["summary"] = summary
        graph["summary_sources"] = list({tuple((evidence[ref]["citation"][key] for key in ("file_path", "start_line", "end_line"))):
                                         evidence[ref]["citation"] for ref in summary_refs}.values())
    graph["nodes"] = list(nodes.values())
    graph["metadata"]["generated_by"] = "deterministic_source_analysis+groq_interpretation"
    if rejected_components or rejected_relationships:
        graph["metadata"]["ai_architecture_status"] = "partially_interpreted"
        graph["metadata"]["ai_architecture_failure_category"] = "unsupported_claims_filtered"
        graph["metadata"]["ai_architecture_rejected_components"] = rejected_components
        graph["metadata"]["ai_architecture_rejected_relationships"] = rejected_relationships
        graph["metadata"]["ai_architecture_rejection_reasons"] = rejection_reasons
        logger.warning("architecture_interpretation_filtered unsupported_components=%s unsupported_relationships=%s repository_id=%s",
                       rejected_components, rejected_relationships, graph["repository"]["id"])
    else:
        graph["metadata"]["ai_architecture_status"] = "interpreted"
    graph["metadata"]["ai_components_added"] = sum(1 for item in graph["nodes"] if item.get("id", "").startswith("ai:"))
    return validate_architecture_graph(graph)


def interpret_architecture_graph(graph: dict, points: Iterable[Any]) -> dict:
    """Use Groq once per cached repository generation; preserve deterministic output on any failure."""
    if not settings.groq_api_key or not settings.groq_model:
        graph["metadata"]["ai_architecture_status"] = "not_configured"
        graph["metadata"]["ai_architecture_failure_category"] = "model_not_configured"
        return graph
    point_list = list(points)
    context, evidence = _source_evidence(point_list)
    if not context or not evidence:
        graph["metadata"]["ai_architecture_status"] = "no_selected_evidence"
        graph["metadata"]["ai_architecture_failure_category"] = "no_source_evidence"
        return graph
    system = (
        "You are RepoInsight's architecture generator. Analyze the repository source excerpts as a connected system, "
        "then return one JSON object only. First identify major runtime components and entry points; next trace "
        "the execution and data flow between them in order; then describe the architecture summary and directed "
        "relationships. This follows a source-context-to-architecture-diagram workflow: produce the meaning a "
        "clear top-to-bottom architecture flowchart should communicate, not a package-import inventory. "
        "Every component and relationship must cite one or more supplied source_refs (E IDs). "
        "Never treat ordinary local libraries such as joblib, NumPy, pandas, scikit-learn, or XGBoost as network services. "
        "Describe referenced binary model files as configured artifact paths, not inspected files. "
        "Identify all source-supported runtime boundaries, entry points, user interfaces, route handlers, storage, integrations, and functional data-processing stages. "
        "For ML repositories, distinguish runtime inference from source-supported training and dataset workflows. "
        "The input includes an existing deterministic graph. Return only source-supported components or relationships that are missing from it; "
        "do not repeat the listed components or edges. Keep at most 8 additional components and 12 additional relationships. "
        "Omit trivial imports. Do not omit a major evidenced workflow stage merely to match a template. "
        "Use compact names and responsibilities (at most 12 words); keep the summary to at most 35 words. "
        "Schema: {summary:string, summary_refs:[E#], components:[{id,name,type,responsibility,technologies:[string],artifact_path?,source_refs:[E#]}], "
        "relationships:[{source_id,target_id,type,label,source_refs:[E#]}]}. "
        "Supported types: layer, component, route, service, artifact, database, external, client. "
        "README text is orientation only and is not implementation evidence. Use only source excerpt evidence IDs to support every component and relationship. "
        "Use only IDs and evidence supplied; do not invent components, API providers, or connections."
    )
    deterministic = [{"id": node["id"], "name": node["label"], "type": node["type"],
                      "artifact_path": node.get("artifact_path"),
                      "sources": [f"{cite['file_path']}:{cite['start_line']}-{cite['end_line']}"
                                  for cite in node.get("citations", [])[:4]]}
                     for node in graph["nodes"] if node["type"] in {"layer", "client", "database", "external"}
                     or node.get("architecture_component")]
    nodes_by_id = {node["id"]: node for node in graph["nodes"]}
    high_level_ids = {node["id"] for node in graph["nodes"] if node["type"] in {"layer", "client", "database", "external"}
                      or node.get("architecture_component")}
    edge_rows = {}
    for edge in graph["edges"]:
        if edge["type"] not in {"api_call", "uses_service", "calls_external", "connects_to", "renders_in"}:
            continue
        source_node, target_node = nodes_by_id[edge["source"]], nodes_by_id[edge["target"]]
        source_id = edge["source"] if edge["source"] in high_level_ids else source_node.get("parent_id")
        target_id = edge["target"] if edge["target"] in high_level_ids else target_node.get("parent_id")
        if not source_id or not target_id or source_id == target_id or source_id not in high_level_ids or target_id not in high_level_ids:
            continue
        key = (source_id, target_id, edge["type"], edge.get("label"))
        if key not in edge_rows:
            edge_rows[key] = {"source_id": source_id, "target_id": target_id, "type": edge["type"],
                              "label": edge.get("label"),
                              "sources": [f"{cite['file_path']}:{cite['start_line']}-{cite['end_line']}"
                                          for cite in edge.get("citations", [])[:2]]}
    deterministic_edges = list(edge_rows.values())[:24]
    readme_context = _readme_context(point_list)
    if readme_context:
        context = f"{context}\n\n{readme_context}"
    user = (f"Repository: {graph['repository']['name']}\nExisting source-verified components: "
            f"{json.dumps(deterministic, ensure_ascii=False)}\nExisting source-verified relationships: "
            f"{json.dumps(deterministic_edges, ensure_ascii=False)}\n\nIndexed source excerpts:\n{context}")
    try:
        raw, finish_reason, _usage = complete_with_metadata(system, user, max_retries=0, max_tokens=3000,
                                                              raise_rate_limit=True, json_mode=True, include_usage=True)
        if finish_reason == "length":
            raise ValueError("truncated_output")
        response = _json_object(raw)
        result = _merge_interpretation(graph, response, evidence)
        if result["metadata"].get("ai_architecture_status") != "partially_interpreted":
            result["metadata"].pop("ai_architecture_failure_category", None)
        if "summary" not in result:
            result["metadata"]["ai_architecture_status"] = "interpreted_without_summary"
        return result
    except GroqRateLimitError:
        logger.warning("architecture_interpretation_unavailable reason=rate_limited repository_id=%s",
                       graph["repository"]["id"])
        graph["metadata"]["ai_architecture_status"] = "rate_limited_deterministic_fallback"
        graph["metadata"]["ai_architecture_failure_category"] = "rate_limited"
    except HTTPException as exc:
        category = "provider_unavailable" if exc.status_code >= 500 else "provider_rejected_request"
        logger.warning("architecture_interpretation_unavailable reason=%s provider_status=%s repository_id=%s",
                       category, exc.status_code, graph["repository"]["id"])
        graph["metadata"]["ai_architecture_status"] = "deterministic_fallback"
        graph["metadata"]["ai_architecture_failure_category"] = category
    except json.JSONDecodeError as exc:
        logger.warning("architecture_interpretation_unavailable reason=invalid_json line=%s column=%s repository_id=%s",
                       exc.lineno, exc.colno, graph["repository"]["id"])
        graph["metadata"]["ai_architecture_status"] = "deterministic_fallback"
        graph["metadata"]["ai_architecture_failure_category"] = "invalid_json"
    except ValueError as exc:
        reason = str(exc)
        category = reason if reason in {"truncated_output", "invalid_components_or_relationships", "response_not_object"} else "evidence_validation_rejected"
        logger.warning("architecture_interpretation_unavailable reason=%s repository_id=%s",
                       category, graph["repository"]["id"])
        graph["metadata"]["ai_architecture_status"] = "deterministic_fallback"
        graph["metadata"]["ai_architecture_failure_category"] = category
    except (TypeError, KeyError) as exc:
        category = "invalid_response_schema"
        logger.warning("architecture_interpretation_unavailable reason=%s error_type=%s repository_id=%s",
                       category, type(exc).__name__, graph["repository"]["id"])
        graph["metadata"]["ai_architecture_status"] = "deterministic_fallback"
        graph["metadata"]["ai_architecture_failure_category"] = category
    except Exception as exc:
        logger.exception("architecture_interpretation_unavailable reason=unexpected_error error_type=%s repository_id=%s",
                         type(exc).__name__, graph["repository"]["id"])
        graph["metadata"]["ai_architecture_status"] = "deterministic_fallback"
        graph["metadata"]["ai_architecture_failure_category"] = "unexpected_provider_or_validation_error"
    return graph
