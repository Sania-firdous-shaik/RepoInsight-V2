"""Build a validated, source-cited architecture graph from indexed repository chunks."""
from __future__ import annotations

from collections import defaultdict
from collections import OrderedDict
import posixpath
import re
import sys
from threading import RLock
from typing import Any, Iterable


NODE_TYPES = {"layer", "component", "route", "middleware", "service", "model", "utility", "client",
              "entrypoint", "configuration", "database", "external", "technology", "module", "artifact"}
EDGE_TYPES = {"imports", "api_call", "uses_middleware", "uses_service", "uses_model",
              "references_model", "connects_to", "calls_external", "implemented_with", "renders_in"}
SOURCE_EXTENSIONS = (".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte",
                     ".java", ".go", ".rs", ".php", ".rb", ".cs", ".sql", ".html", ".htm", ".ipynb",
                     ".ini", ".cfg", ".conf", ".properties")
MAX_MODULES = 320
_GRAPH_CACHE: OrderedDict[tuple[int, str | None, str | None, int], dict] = OrderedDict()
_GRAPH_CACHE_LOCK = RLock()
ARCHITECTURE_CACHE_SCHEMA = 7

IMPORT_PATTERNS = (
    re.compile(r"\b(?:import|export)\s+(?:[^'\";]*?\s+from\s+)?['\"]([^'\"]+)['\"]"),
    re.compile(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(r"^\s*from\s+([.A-Za-z_][\w.]*)\s+import\s+", re.M),
    re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)", re.M),
)
IMPORT_BINDING_PATTERNS = (
    re.compile(r"\b(?:const|let|var)\s+([\w$]+)\s*=\s*require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(r"\bimport\s+([\w$]+)\s+from\s+['\"]([^'\"]+)['\"]"),
)
MOUNT_PATTERN = re.compile(r"\b(?:app|server)\s*\.\s*use\s*\(\s*['\"`]([^'\"`]+)['\"`]\s*,\s*([\w$]+)")
JS_ROUTE_PATTERN = re.compile(r"\b(?:router|app|server)\s*\.\s*(get|post|put|patch|delete|options|head)\s*\(\s*['\"`]([^'\"`]+)", re.I)
PY_ROUTE_PATTERN = re.compile(r"@(?:\w+\.)?(get|post|put|patch|delete|options|head)\s*\(\s*['\"]([^'\"]+)", re.I)
API_CALL_PATTERN = re.compile(
    r"(?:\b(?:api|client|axios)\s*\.\s*(?P<method>get|post|put|patch|delete)|\bfetch\s*)"
    r"\s*\(\s*(?P<quote>['\"`])(?P<url>.*?)(?P=quote)", re.I | re.S
)
URL_PATH_PATTERN = re.compile(r"/(?:api/)?[A-Za-z0-9_./:{}-]+")
HOST_PATTERN = re.compile(r"https?://([A-Za-z0-9.-]+)(?::\d+)?(?:/[^\s'\"`]*)?", re.I)
NETWORK_CALL_PATTERN = re.compile(
    r"\b(?:fetch|axios|requests?|httpx|urlopen)\s*(?:\.\s*(?:get|post|put|patch|delete|request|open))?\s*\(", re.I
)
MODEL_REF_PATTERN = re.compile(r"\bref\s*:\s*['\"]([A-Za-z_$][\w$]*)['\"]")
ARTIFACT_PATH_PATTERN = re.compile(
    r"(?P<variable>[A-Za-z_]\w*)\s*=\s*['\"](?P<path>[^'\"]+\.(?:pkl|pickle|joblib|onnx|pt|pth|h5|keras|safetensors|model))['\"]",
    re.I,
)
ARTIFACT_LOAD_PATTERN = re.compile(r"(?P<target>[A-Za-z_]\w*)\s*=\s*(?:float\s*\(\s*)?(?:joblib|pickle)\s*\.\s*(?:load|loads)\s*\(\s*(?P<source>[A-Za-z_]\w*)", re.I)
DIRECT_ARTIFACT_LOAD_PATTERN = re.compile(
    r"(?:(?P<target>[A-Za-z_]\w*)\s*=\s*)?(?:return\s+)?(?:joblib|pickle)\s*\.\s*(?:load|loads)"
    r"\s*\(\s*['\"](?P<path>[^'\"]+\.(?:pkl|pickle|joblib|onnx|pt|pth|h5|keras|safetensors|model))['\"]",
    re.I,
)
MONGOOSE_PATTERN = re.compile(r"\bmongoose\s*\.\s*(?:connect|model|Schema)|\bSchema\s*\.\s*Types\.\s*ObjectId|mongodb(?:\+srv)?://", re.I)
POSTGRES_PATTERN = re.compile(r"postgres(?:ql)?://|\b(?:psycopg|PostgreSQL|postgresql)\b", re.I)
REDIS_PATTERN = re.compile(r"\bRedis\s*\(|redis://|\bredis\.createClient\b", re.I)
QDRANT_PATTERN = re.compile(r"\bQdrantClient\b|qdrant-client", re.I)
SQL_PATTERN = re.compile(r"\b(?:create_engine|SessionLocal|sqlalchemy)\b", re.I)
BROWSER_CLIENT_PATTERN = re.compile(r"\b(?:BrowserRouter|createBrowserRouter)\b|\bdocument\.(?:createElement|getElementById|querySelector)\b|\bwindow\.(?:location|document)\b|\baddEventListener\s*\(\s*['\"](?:submit|click)|\bfetch\s*\(", re.I)
FLASK_ROUTE_PATTERN = re.compile(r"@(?:\w+\.)?route\s*\(\s*['\"]([^'\"]+)['\"](?P<options>[^)]*)\)", re.I | re.S)
PY_FROM_BINDING_PATTERN = re.compile(r"^\s*from\s+(?P<module>[A-Za-z_][\w.]*)\s+import\s+(?P<name>[A-Za-z_*][\w*]*)(?:\s+as\s+(?P<alias>[A-Za-z_]\w*))?", re.M)
PY_MODULE_BINDING_PATTERN = re.compile(r"^\s*import\s+(?P<module>[A-Za-z_][\w.]*)(?:\s+as\s+(?P<alias>[A-Za-z_]\w*))?", re.M)
NON_SERVICE_SDKS = {"react", "react-dom", "axios", "express", "fastapi", "flask", "django", "mongoose", "mongodb",
                    "pymongo", "motor", "qdrant-client", "sqlalchemy", "psycopg", "redis", "ioredis", "node-fetch",
                    "joblib", "numpy", "pandas", "scipy", "sklearn", "scikit-learn", "xgboost", "lightgbm",
                    "catboost", "torch", "tensorflow", "keras", "onnxruntime", "statsmodels", "matplotlib", "streamlit",
                    "seaborn", "polars", "pydantic", "pytest", "unittest"}
NON_SERVICE_SDKS.update(sys.stdlib_module_names)
NON_SERVICE_SDKS.update({"assert", "buffer", "child_process", "crypto", "events", "fs", "http", "https", "os",
                         "path", "process", "stream", "timers", "url", "util", "zlib"})
SERVICE_SUBDOMAINS = {"www", "api", "checkout", "pay", "payments", "secure", "sandbox", "staging", "smtp", "mail"}
COMMON_TLDS = {"com", "org", "net", "io", "dev", "ai", "app", "co", "in", "uk", "us", "au", "ca", "cloud"}


def _external_service_key(value: str) -> str:
    """Normalize an SDK package or host to a stable provider key."""
    raw = value.lower().strip().strip("/.")
    if raw.startswith("@"):
        raw = raw[1:].split("/", 1)[0]
    elif "/" in raw:
        raw = raw.split("/", 1)[0]
    parts = [part for part in raw.split(".") if part and part not in SERVICE_SUBDOMAINS]
    if len(parts) > 1 and parts[-1] in COMMON_TLDS:
        parts.pop()
    if len(parts) > 1 and parts[-2:] in (["co", "uk"], ["com", "au"], ["co", "in"], ["com", "br"]):
        parts = parts[:-2]
    return parts[-1] if parts else raw


def _provider_label(provider: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_.]+", provider) if part)


def _integration_role(path: str, package: str = "") -> str:
    context = f"{path} {package}".lower()
    if re.search(r"payment|razorpay|stripe|billing|checkout", context):
        return "Payment Gateway"
    if re.search(r"email|mail|smtp|sendgrid|postmark", context):
        return "Email Service"
    if re.search(r"generative|\bai\b|llm|groq|openai|anthropic|gemini", context):
        return "AI API"
    return "API"


def _payload(point: Any) -> dict:
    return point.payload if hasattr(point, "payload") else point.get("payload", {})


def _indexed_line(payload: dict, path: str, field: str, default: int) -> int:
    raw = payload.get(field, default)
    try:
        line = int(raw or default)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid indexed source line metadata for {path!r}: {field}={raw!r}") from exc
    if line < 1:
        raise ValueError(f"Invalid indexed source line metadata for {path!r}: {field}={raw!r}")
    return line


def _source_files(points: Iterable[Any]) -> dict[str, dict]:
    files: dict[str, dict] = {}
    for point in points:
        payload = _payload(point)
        path = str(payload.get("file_path", "")).replace("\\", "/").strip("/")
        if not path:
            continue
        parts = path.lower().split("/")
        basename = parts[-1]
        if path.lower().endswith((".md", ".rst")) or (path.lower().endswith(".txt") and basename != "requirements.txt"):
            continue
        excluded_dirs = {"node_modules", "vendor", "dist", "build", "coverage", ".git", ".venv", "venv",
                         "__pycache__", ".next", "target", "out", "generated", "generated_code", ".cache"}
        if any(part in excluded_dirs for part in parts) or basename.endswith((".min.js", ".generated.js", ".generated.ts")):
            continue
        if basename.endswith(("-lock.json", ".lock", ".min.css")) or basename in {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock", "cargo.lock"}:
            continue
        if not path.lower().endswith(SOURCE_EXTENSIONS + (".json", ".toml", ".yaml", ".yml")) and basename not in {
                "requirements.txt", ".env.example", "dockerfile", "makefile"}:
            continue
        data = files.setdefault(path, {"lines": {}, "symbols": {}, "chunks": []})
        start = _indexed_line(payload, path, "start_line", 1)
        content = str(payload.get("content", ""))
        data["chunks"].append(payload)
        source_line_map = payload.get("source_line_map")
        for offset, line in enumerate(content.splitlines()):
            line_number = (source_line_map[offset] if isinstance(source_line_map, list) and offset < len(source_line_map)
                           and type(source_line_map[offset]) is int else start + offset)
            if line.strip():
                if line_number in data["lines"] and data["lines"][line_number] != line:
                    # Several notebook code lines may be encoded in one JSON
                    # source row; keep both while citing that real row.
                    data["lines"][line_number] += "\n" + line
                else:
                    data["lines"].setdefault(line_number, line)
        symbol = str(payload.get("symbol_name", "")).strip()
        if symbol:
            data["symbols"].setdefault((symbol, str(payload.get("symbol_type", "module")), start,
                                        _indexed_line(payload, path, "end_line", start)), None)
    return files


def _repository_inventory(points: Iterable[Any]) -> set[str] | None:
    """Read a safe index-time manifest; legacy generations report presence as unknown."""
    for point in points:
        inventory = _payload(point).get("repository_file_inventory")
        if isinstance(inventory, list) and all(isinstance(path, str) for path in inventory):
            normalized = {posixpath.normpath(path.replace("\\", "/")) for path in inventory}
            return {path[2:] if path.startswith("./") else path for path in normalized}
    return None


def _citation(path: str, line: int, file_data: dict) -> dict:
    # Architecture evidence is selected from already indexed source lines.
    # Cite the precise line instead of widening it to the containing chunk;
    # build_architecture_graph registers these line references alongside the
    # original indexed chunk ranges before validation.
    if line in file_data.get("lines", {}):
        return {"file_path": path, "start_line": line, "end_line": line}
    chunks = file_data.get("chunks", [])
    matching = [payload for payload in chunks
                if _indexed_line(payload, path, "start_line", 1) <= line <=
                _indexed_line(payload, path, "end_line", _indexed_line(payload, path, "start_line", 1))]
    if matching:
        chunk = min(matching, key=lambda payload: _indexed_line(payload, path, "end_line", _indexed_line(payload, path, "start_line", 1)) - _indexed_line(payload, path, "start_line", 1))
        start = _indexed_line(chunk, path, "start_line", line)
        end = _indexed_line(chunk, path, "end_line", line)
        return {"file_path": path, "start_line": start, "end_line": end}
    return {"file_path": path, "start_line": line, "end_line": line}


def _resolve_import(source_path: str, specifier: str, indexed_paths: set[str]) -> str | None:
    raw = specifier.strip().replace("\\", "/")
    if not raw or raw.startswith(("http:", "https:", "data:")):
        return None
    bases = []
    if raw.startswith("."):
        bases.append(posixpath.normpath(posixpath.join(posixpath.dirname(source_path), raw)))
    else:
        module_path = raw.replace(".", "/")
        bases.extend((module_path, posixpath.join(posixpath.dirname(source_path), module_path)))
    candidates = []
    for base in bases:
        candidates.append(base)
        if not posixpath.splitext(base)[1]:
            candidates.extend(base + ext for ext in SOURCE_EXTENSIONS)
            candidates.extend(posixpath.join(base, "index" + ext) for ext in (".js", ".jsx", ".ts", ".tsx", ".py"))
        else:
            candidates.append(base)
    normalized = {path.lower(): path for path in indexed_paths}
    for candidate in candidates:
        found = normalized.get(posixpath.normpath(candidate).lower())
        if found:
            return found
    suffixes = [posixpath.normpath(candidate).lower() for candidate in candidates]
    matches = [path for path in indexed_paths if any(path.lower().endswith("/" + suffix) for suffix in suffixes)]
    return min(matches, key=lambda path: (len(path), path.lower())) if matches else None


def _role(path: str, file_data: dict) -> tuple[str, str]:
    normalized = path.lower().replace("\\", "/")
    parts = normalized.split("/")
    stem = posixpath.basename(normalized).rsplit(".", 1)[0]
    chunks = file_data.get("chunks", [])
    types = {str(chunk.get("symbol_type", "")).lower() for chunk in chunks}
    content = "\n".join(file_data.get("lines", {}).values())
    if "routes" in parts or "route" in types:
        return "route", "Indexed API route handlers and endpoint definitions."
    if "middleware" in parts:
        return "middleware", "Indexed request middleware."
    if "models" in parts or "model" in types:
        return "model", "Indexed persistence model or schema."
    if "services" in parts or "service" in types:
        return "service", "Indexed service implementation."
    if "utils" in parts or "utilities" in parts:
        return "utility", "Indexed shared utility module."
    if posixpath.basename(normalized) in {"package.json", "pyproject.toml", "requirements.txt", "go.mod", "cargo.toml", "pom.xml", ".env.example"} or "config" in parts:
        return "configuration", "Indexed project configuration or dependency manifest."
    if stem in {"index", "main", "app", "server", "manage"} and any(token in content.lower() for token in ("express", "fastapi", "flask", "streamlit", "createapp", "create_app", "createRoot", "BrowserRouter", "createBrowserRouter", "joblib.load", "predict_proba")):
        return "entrypoint", "Indexed application entry point."
    if normalized.endswith((".jsx", ".tsx", ".vue", ".svelte")) or any(part in parts for part in ("frontend", "client", "web", "components", "pages")):
        return "component", "Indexed frontend component or page."
    if normalized.endswith((".html", ".htm")) and re.search(r"<\s*(?:form|script|input|button|main|body)\b", content, re.I):
        return "component", "Indexed browser UI template."
    if any(pattern.search(content) for pattern in (MONGOOSE_PATTERN, POSTGRES_PATTERN, REDIS_PATTERN, QDRANT_PATTERN, SQL_PATTERN)):
        return "service", "Indexed module containing a database or infrastructure client."
    return "module", "Indexed source module."


def _layer(path: str, role: str, file_data: dict) -> str:
    normalized = path.lower().replace("\\", "/")
    parts = normalized.split("/")
    has_frontend_segment = any(re.search(r"(^|[-_.])(frontend|client|web|ui)([-_.]|$)", part) for part in parts)
    has_backend_segment = any(re.search(r"(^|[-_.])(backend|server|api)([-_.]|$)", part) for part in parts)
    if role == "component" or has_frontend_segment:
        return "frontend"
    content = "\n".join(file_data.get("lines", {}).values()).lower()
    if (role in {"route", "middleware", "service", "model", "utility", "entrypoint"}
            or has_backend_segment
            or any(term in content for term in ("fastapi", "flask(", "streamlit", "express()", "router.", "mongoose.connect", "create_engine",
                                                "joblib.load", "predict_proba", "preprocessor.transform"))):
        return "backend"
    return "shared"


def _line_records(file_data: dict):
    return sorted(file_data.get("lines", {}).items())


def _indexed_content(file_data: dict) -> tuple[str, list[int]]:
    """Join indexed non-empty lines while retaining their original source line numbers."""
    records = _line_records(file_data)
    return "\n".join(line for _, line in records), [line_number for line_number, _ in records]


def _line_at_offset(content: str, offset: int, source_line_numbers: list[int]) -> int:
    """Map an offset in _indexed_content() back to the original indexed line."""
    if not source_line_numbers:
        return 1
    index = content.count("\n", 0, max(0, offset))
    return source_line_numbers[min(index, len(source_line_numbers) - 1)]


def _add_ml_pipeline(repository_paths: list[str], files: dict[str, dict], metadata: dict[str, dict],
                     nodes: list[dict], edge_map: dict[tuple, dict],
                     repository_inventory: set[str] | None = None) -> None:
    """Promote source-verified ML inference stages and referenced artifact files."""
    for path in repository_paths:
        lines = _line_records(files[path])
        source, source_line_numbers = _indexed_content(files[path])
        if not re.search(r"\b(?:joblib|pickle)\s*\.\s*(?:load|loads)\s*\(", source, re.I):
            continue
        if not re.search(r"\.\s*(?:predict|predict_proba|decision_function)\s*\(", source, re.I):
            continue
        if metadata[path]["parent_id"] != "layer:backend":
            continue

        variables: dict[str, tuple[str, int]] = {}
        for match in ARTIFACT_PATH_PATTERN.finditer(source):
            line_number = _line_at_offset(source, match.start(), source_line_numbers)
            variables[match.group("variable")] = (match.group("path"), line_number)
        loaded: dict[str, tuple[str, str, int, int]] = {}
        for match in ARTIFACT_LOAD_PATTERN.finditer(source):
            configured = variables.get(match.group("source"))
            if not configured:
                continue
            load_line = _line_at_offset(source, match.start(), source_line_numbers)
            loaded[match.group("target")] = (configured[0], match.group("source"), configured[1], load_line)
        # Also recognize a path passed directly to a loader, commonly inside
        # a helper such as ``return joblib.load("model.joblib")``.
        for match in DIRECT_ARTIFACT_LOAD_PATTERN.finditer(source):
            artifact_path = match.group("path").replace("\\", "/")
            load_line = _line_at_offset(source, match.start(), source_line_numbers)
            key = match.group("target") or f"__direct_{artifact_path}"
            loaded.setdefault(key, (artifact_path, "", load_line, load_line))

        artifacts: dict[str, dict] = {}
        for variable, (artifact_path, configured_line, load_line) in (
            (name, (value[0], value[2], value[3])) for name, value in loaded.items()
        ):
            name = posixpath.basename(artifact_path).lower()
            if re.search(r"preprocess|transform|scaler|encoder", name + " " + variable.lower()):
                key, label, responsibility = "preprocessor", "Data Preprocessor", "Configured preprocessing artifact loaded from the referenced model path."
            elif re.search(r"threshold|cutoff|decision", name + " " + variable.lower()):
                key, label, responsibility = "threshold", "Threshold & Risk Evaluation", "Configured threshold artifact used to classify model probability and risk."
            elif re.search(r"model|xgb|classifier|estimator", name + " " + variable.lower()):
                key, label, responsibility = "model", ("XGBoost Model" if "xgboost" in name or "xgb" in name else "ML Model"), "Configured model artifact loaded and used for prediction."
            else:
                continue
            if repository_inventory is None:
                artifact_exists = None
            else:
                candidates = [posixpath.normpath(artifact_path),
                              posixpath.normpath(posixpath.join(posixpath.dirname(path), artifact_path))]
                artifact_path = next((candidate for candidate in candidates if candidate in repository_inventory),
                                     artifact_path)
                artifact_exists = artifact_path in repository_inventory
            citation_lines = [configured_line, load_line]
            if key == "preprocessor":
                calls = [n for n, line in lines if re.search(rf"\b{re.escape(variable)}\s*\.\s*transform\s*\(", line)]
                citation_lines.extend(calls[:1])
            elif key == "model":
                calls = [n for n, line in lines if re.search(rf"\b{re.escape(variable)}\s*\.\s*(?:predict|predict_proba|decision_function)\s*\(", line)]
                citation_lines.extend(calls[:1])
            elif key == "threshold":
                comparisons = [n for n, line in lines if re.search(rf"\b(?:probability|score|prediction)\s*(?:>=|>)\s*{re.escape(variable)}\b", line, re.I)]
                citation_lines.extend(comparisons[:1])
            citations = []
            for line_number in citation_lines:
                citation = _citation(path, line_number, files[path])
                if citation not in citations:
                    citations.append(citation)
            node_id = f"artifact:{artifact_path}"
            display_label = (f"{label} · {posixpath.basename(artifact_path)}" if artifact_exists else
                             f"Configured model path · {posixpath.basename(artifact_path)}")
            if artifact_exists is True:
                artifact_status = "Artifact file exists in the indexed repository file inventory."
            elif artifact_exists is False:
                artifact_status = "Source references this path, but it is absent from the indexed repository file inventory."
            else:
                artifact_status = "Source references this path; this legacy index cannot verify whether the file exists."
            artifact = {"id": node_id, "label": display_label, "type": "artifact", "parent_id": None,
                        "path": None, "artifact_path": artifact_path, "architecture_component": True,
                        "artifact_exists": artifact_exists,
                        "architecture_order": {"preprocessor": 2, "model": 3, "threshold": 4}[key],
                        "responsibility": f"{responsibility} {artifact_status}", "symbols": [], "routes": [], "citations": citations,
                        "expandable": False}
            if artifact_exists is True and key == "threshold":
                artifact["label"] = f"Threshold & Risk Evaluation · {posixpath.basename(artifact_path)}"
            elif artifact_exists is True and key == "preprocessor":
                artifact["label"] = f"Data Preprocessor · {posixpath.basename(artifact_path)}"
            nodes.append(artifact)
            artifacts[key] = artifact

        feature_def = next((n for n, line in lines if re.search(
            r"^\s*def\s+[A-Za-z_]\w*(?:feature|transform|distance|derive|calculate)\w*\s*\(", line, re.I)), None)
        feature_call = next((n for n, line in lines if re.search(
            r"\b[A-Za-z_]\w*(?:feature|transform|distance|derive|calculate)\w*\s*\(", line, re.I)
            and not re.match(r"\s*def\b", line)), None)
        frame_line = next((n for n, line in lines if re.search(r"\b(?:pd\.)?DataFrame\s*\(", line)), None)
        input_lines = [n for n, line in lines if re.search(
            r"\bst\.(?:form|form_submit_button|number_input|selectbox|text_input|slider|checkbox)\s*\(|"
            r"\brequest\.(?:form|json|args)\b", line, re.I)]
        derived_feature_lines = [n for n, line in lines if re.search(
            r"['\"]?[A-Za-z_]\w*(?:_per_[A-Za-z_]\w*|_ratio|_distance|_derived|_engineered)['\"]?\s*:"
            r"|\b[A-Za-z_]\w*\s*/\s*\([^\n)]*\+\s*1e-\d+\s*\)", line, re.I)]
        dataframe_line = next((n for n, line in lines if re.search(r"\b(?:pd\.)?DataFrame\s*\(", line)), None)
        feature_evidence = (feature_def or feature_call or derived_feature_lines or re.search(
            r"\b(?:engineer|extract|build|derive)_features\b|\.\s*assign\s*\(", source, re.I))
        feature_node = None
        if feature_evidence:
            feature_lines = [n for n in (feature_def, feature_call, dataframe_line, *derived_feature_lines) if n]
            feature_citations = [_citation(path, n, files[path]) for n in feature_lines[:6]]
            unique = list({(item["file_path"], item["start_line"], item["end_line"]): item for item in feature_citations}.values())
            feature_node = {"id": f"process:{path}:feature-engineering", "label": "Feature Engineering",
                            "type": "service", "parent_id": None, "path": None, "architecture_component": True,
                            "architecture_order": 1,
                            "responsibility": "Derives model input features using transformations present in the indexed source.",
                            "symbols": [], "routes": [], "citations": unique, "expandable": False}
            nodes.append(feature_node)

        preprocessor, model, threshold = (artifacts.get(key) for key in ("preprocessor", "model", "threshold"))
        previous = feature_node
        if feature_node:
            if not input_lines:
                _add_edge(edge_map, metadata[path]["parent_id"], feature_node["id"], "uses_service",
                          feature_node["citations"], label="engineers features")
        if previous and preprocessor:
            cites = [_citation(path, next((n for n, line in lines if re.search(r"\binput_data\s*=\s*pd\.DataFrame", line)), frame_line or 1), files[path]),
                     *preprocessor["citations"]]
            _add_edge(edge_map, previous["id"], preprocessor["id"], "uses_service", cites,
                      label="engineered features")
            previous = preprocessor
        elif preprocessor:
            _add_edge(edge_map, metadata[path]["parent_id"], preprocessor["id"], "uses_service",
                      preprocessor["citations"], label="preprocesses input")
            previous = preprocessor
        if not previous and model:
            _add_edge(edge_map, metadata[path]["parent_id"], model["id"], "uses_service",
                      model["citations"], label="runs inference")
            previous = model
        if previous and model and previous["id"] != model["id"]:
            _add_edge(edge_map, previous["id"], model["id"], "uses_service", model["citations"],
                      label="transformed features")
            previous = model
        if previous and threshold:
            _add_edge(edge_map, previous["id"], threshold["id"], "uses_service", threshold["citations"],
                      label="prediction probability")
            previous = threshold
        if previous:
            backend_id = metadata[path]["parent_id"]
            _add_edge(edge_map, previous["id"], backend_id, "uses_service", previous["citations"],
                      label="risk result")

        input_node = None
        if input_lines:
            input_citations = [_citation(path, n, files[path]) for n in input_lines[:8]]
            streamlit_input = any("st." in line for n, line in lines if n in input_lines)
            input_node = {"id": f"process:{path}:user-input",
                          "label": "User Input Form" if streamlit_input else "Prediction Request",
                          "type": "component", "parent_id": None, "path": None,
                          "architecture_component": True,
                          "responsibility": "Collects submitted values used by the indexed prediction workflow.",
                          "symbols": [], "routes": [], "citations": input_citations, "expandable": False}
            nodes.append(input_node)
            _add_edge(edge_map, metadata[path]["parent_id"], input_node["id"], "uses_service",
                      input_citations, label="accepts input")
            if feature_node:
                _add_edge(edge_map, input_node["id"], feature_node["id"], "uses_service",
                          [*input_citations[:2], *feature_node["citations"][:2]], label="builds model input")

        inference_lines = [n for n, line in lines if re.search(
            r"\b[A-Za-z_]\w*\s*\.\s*(?:predict|predict_proba|decision_function)\s*\(", line, re.I)]
        inference_node = None
        if inference_lines and (model or loaded):
            inference_citations = [_citation(path, n, files[path]) for n in inference_lines[:4]]
            inference_node = {"id": f"process:{path}:model-inference", "label": "Model Inference",
                              "type": "service", "parent_id": None, "path": None,
                              "architecture_component": True,
                              "responsibility": "Runs the loaded model against prepared application input.",
                              "symbols": [], "routes": [], "citations": inference_citations, "expandable": False}
            nodes.append(inference_node)
            if feature_node:
                _add_edge(edge_map, feature_node["id"], inference_node["id"], "uses_model",
                          [*feature_node["citations"][:2], *inference_citations[:2]], label="predicts from features")
            elif input_node:
                _add_edge(edge_map, input_node["id"], inference_node["id"], "uses_model",
                          [*input_node["citations"][:2], *inference_citations[:2]], label="runs prediction")
            for artifact in artifacts.values():
                if artifact.get("architecture_order") == 3:
                    _add_edge(edge_map, artifact["id"], inference_node["id"], "uses_model",
                              [*artifact["citations"][:2], *inference_citations[:2]], label="loaded model")

        if inference_node:
            inference_line = inference_lines[0]
            output_lines = [n for n, line in lines if n >= inference_line and re.search(
                r"\bst\.(?:write|markdown|metric|progress|success|warning|error)\s*\(|"
                r"\b(?:jsonify|render_template)\s*\(|\breturn\s+", line, re.I)
                and (n - inference_line <= 110)]
            source_by_line = dict(lines)
            result_title = next((n for n in output_lines if re.search(
                r"screening results|prediction results|risk results", source_by_line.get(n, ""), re.I)), None)
            if result_title:
                output_lines = [result_title, *[n for n in output_lines if n > result_title]][:6]
            if output_lines:
                output_citations = [_citation(path, n, files[path]) for n in output_lines[:6]]
                output_node = {"id": f"process:{path}:prediction-output", "label": "Prediction Results",
                               "type": "component", "parent_id": None, "path": None,
                               "architecture_component": True,
                               "responsibility": "Presents the model prediction through the indexed application interface.",
                               "symbols": [], "routes": [], "citations": output_citations, "expandable": False}
                nodes.append(output_node)
                _add_edge(edge_map, inference_node["id"], output_node["id"], "uses_service",
                          [*inference_node["citations"][:2], *output_citations[:2]], label="displays prediction")
        break


def _add_ml_development_flow(repository_paths: list[str], files: dict[str, dict],
                             nodes: list[dict], edge_map: dict[tuple, dict],
                             repository_inventory: set[str] | None = None) -> None:
    """Represent source-evidenced dataset and training stages alongside runtime inference."""
    dataset_pattern = re.compile(r"(?:pd\.)?read_(?:csv|parquet|json)\s*\(\s*\\?['\"]([^'\"]+)\\?['\"]", re.I)
    train_pattern = re.compile(r"\.(?:fit|fit_transform)\s*\(|\btrain_test_split\s*\(|\bjoblib\.dump\s*\(|\bpickle\.dump\s*\(", re.I)
    model_artifacts = [node for node in nodes if node.get("type") == "artifact"
                       and re.search(r"model|xgb|classifier|estimator", node.get("artifact_path", ""), re.I)]

    for path in repository_paths:
        source_lines = _line_records(files[path])
        source = "\n".join(line for _, line in source_lines)
        datasets = []
        for match in dataset_pattern.finditer(source):
            line_number = _line_at_offset(source, match.start(), [number for number, _ in source_lines])
            reference = match.group(1).replace("\\", "/")
            candidates = [posixpath.normpath(reference),
                          posixpath.normpath(posixpath.join(posixpath.dirname(path), reference))]
            candidates = [candidate[2:] if candidate.startswith("./") else candidate for candidate in candidates]
            valid_candidates = [candidate for candidate in candidates if not candidate.startswith("../")]
            found_path = next((candidate for candidate in valid_candidates
                               if repository_inventory is not None and candidate in repository_inventory), None)
            data_path = found_path or (valid_candidates[0] if valid_candidates else candidates[-1])
            data_exists = found_path is not None if repository_inventory is not None else None
            cite = _citation(path, line_number, files[path])
            node_id = f"artifact:dataset:{data_path}"
            dataset = next((node for node in nodes if node["id"] == node_id), None)
            if dataset is None:
                if data_exists is True:
                    label = f"Dataset · {posixpath.basename(data_path)}"
                    existence = "Dataset file exists in the indexed repository file inventory."
                elif data_exists is False:
                    label = f"Configured data path · {posixpath.basename(data_path)}"
                    existence = "Source references this path, but it is absent from the indexed repository file inventory."
                else:
                    label = f"Configured data path · {posixpath.basename(data_path)}"
                    existence = "Source references this path; this legacy index cannot verify whether the file exists."
                dataset = {"id": node_id, "label": label,
                           "type": "artifact", "parent_id": None, "path": None,
                           "artifact_path": data_path, "artifact_exists": data_exists, "architecture_component": True,
                           "responsibility": f"Dataset path passed to an indexed data-loader. {existence}",
                           "symbols": [], "routes": [], "citations": [cite], "expandable": False}
                nodes.append(dataset)
            elif cite not in dataset["citations"]:
                dataset["citations"].append(cite)
            datasets.append((dataset, cite))

        train_match = train_pattern.search(source)
        if not train_match:
            continue
        train_line = _line_at_offset(source, train_match.start(), [number for number, _ in source_lines])
        training_citations = [_citation(path, train_line, files[path])]
        notebook = path.lower().endswith(".ipynb")
        training_node = {"id": f"process:{path}:model-development",
                         "label": "Jupyter Notebook Model Development" if notebook else "Model Development",
                         "type": "service", "parent_id": None, "path": None,
                         "architecture_component": True, "responsibility": "Trains or fits a model using the indexed development workflow.",
                         "symbols": [], "routes": [], "citations": training_citations, "expandable": False}
        if not any(node["id"] == training_node["id"] for node in nodes):
            nodes.append(training_node)
        for dataset, cite in datasets:
            _add_edge(edge_map, dataset["id"], training_node["id"], "uses_service", [cite, training_citations[0]],
                      label="training data")

        output_path = None
        save_match = re.search(r"(?:joblib|pickle)\.dump\s*\([^,]+,\s*['\"]([^'\"]+)['\"]", source, re.I)
        if save_match:
            output_path = save_match.group(1).replace("\\", "/")
        target = next((node for node in model_artifacts if output_path and node.get("artifact_path") == output_path), None)
        if target:
            _add_edge(edge_map, training_node["id"], target["id"], "uses_model",
                      [training_citations[0], *target.get("citations", [])[:1]], label="trained model artifact")


def _add_streamlit_user_flow(nodes: list[dict], edge_map: dict[tuple, dict]) -> None:
    """Show the browser-facing user boundary for a verified Streamlit application."""
    streamlit = next((node for node in nodes if node.get("id") == "technology:streamlit"), None)
    application = next((node for node in nodes if node.get("id") == "layer:backend"
                        and "streamlit" in node.get("label", "").lower()), None)
    if not streamlit or not application:
        return
    citations = list({(item["file_path"], item["start_line"], item["end_line"]): item
                     for item in [*streamlit.get("citations", []), *application.get("citations", [])]}.values())
    if not citations:
        return
    client = {"id": "client:browser", "label": "User / Web Browser", "type": "client", "parent_id": None,
              "path": None, "responsibility": "User interacts with the source-verified Streamlit interface.",
              "symbols": [], "routes": [], "citations": citations, "expandable": False}
    if not any(node.get("id") == client["id"] for node in nodes):
        nodes.append(client)
    _add_edge(edge_map, client["id"], application["id"], "renders_in", citations, label="user interaction")


def _add_edge(edge_map: dict, source: str, target: str, relationship: str,
              citations: list[dict], verified: bool = True, label: str | None = None) -> None:
    if source == target or not citations or relationship not in EDGE_TYPES:
        return
    key = (source, target, relationship)
    current = edge_map.setdefault(key, {"id": f"edge:{len(edge_map) + 1}", "source": source, "target": target,
                                       "type": relationship, "verified": verified, "citations": [], "label": label})
    if label and not current.get("label"):
        current["label"] = label
    for citation in citations:
        marker = (citation["file_path"], citation["start_line"], citation["end_line"])
        if marker not in {(item["file_path"], item["start_line"], item["end_line"]) for item in current["citations"]}:
            current["citations"].append(citation)


def validate_architecture_graph(graph: dict) -> dict:
    """Validate graph identities and evidence references before an API response is rendered."""
    if not isinstance(graph, dict) or graph.get("version") != 1:
        raise ValueError("Unsupported architecture graph payload")
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    sources = graph.get("sources")
    if not isinstance(nodes, list) or not isinstance(edges, list) or not isinstance(sources, list):
        raise ValueError("Architecture graph nodes, edges, and sources must be arrays")
    source_keys = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ValueError(f"Source evidence #{index + 1} is not an object")
        path, start, end = source.get("file_path"), source.get("start_line"), source.get("end_line")
        if not isinstance(path, str) or not path.strip() or type(start) is not int or type(end) is not int or start < 1 or end < start:
            raise ValueError(f"Invalid source evidence #{index + 1}: file_path={path!r} lines={start!r}-{end!r}")
        source_keys.add((path, start, end))
    node_map: dict[str, dict] = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"].strip():
            raise ValueError("Architecture nodes require stable non-empty ids")
        if node["id"] in node_map:
            raise ValueError(f"Duplicate architecture node id: {node['id']}")
        if node.get("type") not in NODE_TYPES:
            raise ValueError(f"Unsupported architecture node type: {node.get('type')}")
        if node.get("parent_id") and node["parent_id"] == node["id"]:
            raise ValueError(f"Architecture node {node['id']!r} cannot parent itself")
        for citation in node.get("citations", []):
            if not isinstance(citation, dict):
                raise ValueError(f"Architecture node {node['id']!r} has a non-object citation: {citation!r}")
            citation_key = (citation.get("file_path"), citation.get("start_line"), citation.get("end_line"))
            if citation_key not in source_keys:
                raise ValueError(f"Architecture node {node['id']!r} citation is absent from graph sources: "
                                 f"{citation_key[0]!r} lines {citation_key[1]!r}-{citation_key[2]!r}")
        node_map[node["id"]] = node
    for node in nodes:
        if node.get("parent_id") and node["parent_id"] not in node_map:
            raise ValueError(f"Architecture node {node['id']!r} references missing parent {node['parent_id']!r}")
    edge_ids = set()
    for edge in edges:
        if not isinstance(edge, dict) or not edge.get("id") or edge["id"] in edge_ids:
            edge_id = edge.get("id") if isinstance(edge, dict) else None
            raise ValueError(f"Architecture edge has a missing or duplicate id: {edge_id!r}")
        if edge.get("source") not in node_map or edge.get("target") not in node_map:
            raise ValueError(f"Architecture edge {edge['id']!r} has missing endpoint: "
                             f"{edge.get('source')!r} -> {edge.get('target')!r}")
        if edge.get("type") not in EDGE_TYPES:
            raise ValueError(f"Unsupported architecture relationship on edge {edge['id']!r}: {edge.get('type')!r}")
        if not edge.get("verified") or not edge.get("citations"):
            raise ValueError(f"Architecture edge {edge['id']!r} ({edge.get('source')!r} -> {edge.get('target')!r}) "
                             "requires verified source evidence")
        for citation in edge["citations"]:
            if not isinstance(citation, dict):
                raise ValueError(f"Architecture edge {edge['id']!r} has a non-object citation: {citation!r}")
            citation_key = (citation.get("file_path"), citation.get("start_line"), citation.get("end_line"))
            if citation_key not in source_keys:
                raise ValueError(f"Architecture edge {edge['id']!r} citation is absent from graph sources: "
                                 f"{citation_key[0]!r} lines {citation_key[1]!r}-{citation_key[2]!r}")
        edge_ids.add(edge["id"])
    return graph


def build_architecture_graph(repository_name: str, repository_id: int, points: Iterable[Any]) -> dict:
    point_list = list(points)
    files = _source_files(point_list)
    repository_inventory = _repository_inventory(point_list)
    paths = set(files)
    module_paths = sorted(paths, key=lambda path: (path.lower(), path))[:MAX_MODULES]
    omitted_count = max(0, len(paths) - len(module_paths))
    module_path_set = set(module_paths)
    metadata: dict[str, dict] = {}
    source_records: dict[tuple[str, int, int], dict] = {}
    for path in module_paths:
        kind, responsibility = _role(path, files[path])
        layer = _layer(path, kind, files[path])
        citations = []
        for payload in files[path]["chunks"]:
            start = _indexed_line(payload, path, "start_line", 1)
            end = _indexed_line(payload, path, "end_line", start)
            cite = {"file_path": path, "start_line": start, "end_line": end}
            source_records[(path, start, end)] = cite
            if cite not in citations:
                citations.append(cite)
        citations.sort(key=lambda item: (item["start_line"], item["end_line"]))
        metadata[path] = {"id": f"file:{path}", "label": posixpath.basename(path), "type": kind,
                          "parent_id": f"layer:{layer}", "path": path, "responsibility": responsibility,
                          "symbols": [{"name": symbol, "type": symbol_type, "file_path": path,
                                       "start_line": start, "end_line": end}
                                      for symbol, symbol_type, start, end in files[path]["symbols"]][:40],
                          "routes": [], "citations": citations[:40], "expandable": False}

    nodes: list[dict] = []
    layers: dict[str, dict] = {}
    for path in module_paths:
        module = metadata[path]
        layer_id = module["parent_id"]
        layer_type = layer_id.split(":", 1)[1]
        has_backend_routes = layer_type == "backend" and any(
            item["type"] == "route" and item["parent_id"] == layer_id for item in metadata.values())
        has_other_application_layers = any(item["parent_id"] != "layer:shared" for item in metadata.values())
        layer = layers.setdefault(layer_type, {"id": layer_id,
            "label": {"frontend": "Frontend application", "backend": "Backend API" if has_backend_routes else "Backend application",
                      "shared": "Shared modules" if has_other_application_layers else "Application modules"}[layer_type],
            "type": "layer", "parent_id": None, "responsibility": "Expand to inspect indexed source modules.",
            "module_count": 0, "citations": [], "expandable": True})
        layer["module_count"] += 1
        for cite in module["citations"][:2]:
            if cite not in layer["citations"]:
                layer["citations"].append(cite)
        nodes.append(module)

    # Promote verified runtime data stores and external hosts to explicit graph nodes.
    database_nodes: dict[str, dict] = {}
    external_nodes: dict[str, dict] = {}
    external_keys_by_host: dict[str, str] = {}
    sdk_integrations: list[tuple[str, str, list[dict], str]] = []
    technology_nodes: dict[str, dict] = {}
    for path in module_paths:
        content, content_line_numbers = _indexed_content(files[path])
        for pattern, label in ((MONGOOSE_PATTERN, "MongoDB"), (POSTGRES_PATTERN, "PostgreSQL"),
                               (REDIS_PATTERN, "Redis"), (QDRANT_PATTERN, "Qdrant"), (SQL_PATTERN, "Configured SQL database")):
            match = pattern.search(content)
            if not match:
                continue
            key = label.lower()
            if key not in database_nodes:
                database_nodes[key] = {"id": f"database:{key.replace(' ', '-')}", "label": label, "type": "database",
                    "parent_id": None, "path": None, "responsibility": "Database or infrastructure client referenced by indexed source.",
                    "symbols": [], "routes": [], "citations": [], "expandable": False}
            line_number = next((number for number, line in _line_records(files[path]) if pattern.search(line)), 1)
            cite = _citation(path, line_number, files[path])
            if cite not in database_nodes[key]["citations"]:
                database_nodes[key]["citations"].append(cite)
        if posixpath.basename(path).lower() == "package.json":
            for package, label, layer_name in (("react", "React", "frontend"), ("vite", "Vite", "frontend"),
                                               ("tailwindcss", "Tailwind CSS", "frontend"), ("express", "Express", "backend"),
                                               ("mongoose", "Mongoose", "backend"), ("fastapi", "FastAPI", "backend"),
                                               ("sqlalchemy", "SQLAlchemy", "backend")):
                package_match = re.search(rf"['\"]{re.escape(package)}['\"]\s*:", content, re.I)
                if package_match:
                    line_number = _line_at_offset(content, package_match.start(), content_line_numbers)
                    # Package manifests can be pretty-printed; use the precise indexed row when possible.
                    line_number = next((n for n, line in _line_records(files[path]) if package_match.group(0) in line.lower()), line_number)
                    cite = _citation(path, line_number, files[path])
                    tech = technology_nodes.setdefault(label, {"id": f"technology:{label.lower()}", "label": label,
                        "type": "technology", "parent_id": None, "path": path, "layer_name": layer_name,
                        "responsibility": "Dependency declared by an indexed package manifest.", "symbols": [], "routes": [],
                        "citations": [], "expandable": False})
                    if cite not in tech["citations"]:
                        tech["citations"].append(cite)
            source_lines = files[path]["lines"]
            source_text = "\n".join(source_lines.get(number, "")
                                    for number in range(1, max(source_lines, default=0) + 1))
            runtime_match = re.search(
                r'"scripts"\s*:\s*\{[^}]*"[^"\n]+"\s*:\s*"[^"\n]*\b(?:node|nodemon)\b'
                r'|"engines"\s*:\s*\{[^}]*"node"\s*:', source_text, re.I | re.S)
            if runtime_match:
                line_number = source_text.count("\n", 0, runtime_match.start()) + 1
                cite = _citation(path, line_number, files[path])
                tech = technology_nodes.setdefault("Node.js", {"id": "technology:nodejs", "label": "Node.js",
                    "type": "technology", "parent_id": None, "path": path,
                    "responsibility": "Runtime declared by an indexed package script or engine constraint.",
                    "symbols": [], "routes": [], "citations": [], "expandable": False})
                if cite not in tech["citations"]:
                    tech["citations"].append(cite)
        framework_rules = (
            ("Flask", re.compile(r"\bfrom\s+flask\s+import\b|\bFlask\s*\(", re.I)),
            ("FastAPI", re.compile(r"\bfrom\s+fastapi\s+import\b|\bFastAPI\s*\(", re.I)),
            ("Streamlit", re.compile(r"\bimport\s+streamlit\b|\bfrom\s+streamlit\s+import\b|\bst\.(?:title|form|button|text_input)\s*\(", re.I)),
            ("Django", re.compile(r"\bfrom\s+django\b|\bDjango\b", re.I)),
            ("Express", re.compile(r"\b(?:require\s*\(\s*['\"]express['\"]\s*\)|from\s+['\"]express['\"])", re.I)),
        )
        for label, pattern in framework_rules:
            match = pattern.search(content)
            if match:
                line_number = _line_at_offset(content, match.start(), content_line_numbers)
                cite = _citation(path, line_number, files[path])
                tech = technology_nodes.setdefault(label, {"id": f"technology:{label.lower()}", "label": label,
                    "type": "technology", "parent_id": None, "path": path, "layer_name": metadata[path]["parent_id"].split(":", 1)[1],
                    "responsibility": "Application framework referenced by indexed source code.",
                    "symbols": [], "routes": [], "citations": [], "expandable": False})
                if cite not in tech["citations"]:
                    tech["citations"].append(cite)
        if path.lower().endswith((".html", ".htm")) and re.search(r"<\s*script\b", content, re.I):
            match = re.search(r"<\s*script\b", content, re.I)
            line_number = _line_at_offset(content, match.start(), content_line_numbers)
            cite = _citation(path, line_number, files[path])
            tech = technology_nodes.setdefault("HTML / JavaScript UI", {"id": "technology:html-javascript-ui",
                "label": "HTML / JavaScript UI", "type": "technology", "parent_id": None, "path": path, "layer_name": "frontend",
                "responsibility": "Browser interface and script are present in the indexed template.",
                "symbols": [], "routes": [], "citations": [], "expandable": False})
            if cite not in tech["citations"]:
                tech["citations"].append(cite)
        for line_number, line in _line_records(files[path]):
            # A URL mentioned in a comment is documentation, not evidence that
            # the application connects to that host (for example Vite's docs).
            if line.lstrip().startswith(("//", "#", "*", "<!--")):
                continue
            for host in HOST_PATTERN.findall(line):
                host = host.lower().rstrip(".")
                # Avoid turning local and repository URLs into architecture integrations.
                if host in {"localhost", "127.0.0.1", "github.com", "raw.githubusercontent.com"}:
                    continue
                # A URL by itself can be a documentation link, image, font, or
                # static asset. Promote it as a service only when source code
                # actually passes that URL to a network client. SDK-backed
                # integrations are independently discovered below.
                if not NETWORK_CALL_PATTERN.search(line) and _integration_role(path, host) == "API":
                    continue
                key = _external_service_key(host)
                external_keys_by_host[host] = key
                provider = _provider_label(key)
                host_parts = host.split(".")
                host_role = " Checkout" if host_parts[0] == "checkout" else ""
                external = external_nodes.setdefault(key, {"id": f"external:{key}", "label": f"{provider}{host_role}",
                    "type": "external", "parent_id": None, "path": None,
                    "hostnames": [], "packages": [], "integration_role": _integration_role(path, host),
                    "responsibility": "External service referenced by indexed source.",
                    "symbols": [], "routes": [], "citations": [], "expandable": False})
                if host not in external["hostnames"]:
                    external["hostnames"].append(host)
                cite = _citation(path, line_number, files[path])
                if cite not in external["citations"]:
                    external["citations"].append(cite)

        # Discover third-party SDK clients only when indexed code imports a
        # package, constructs/configures its client, and invokes that client.
        # This covers SDK-backed integrations whose source does not contain a
        # public host URL (for example payment, email, and model APIs).
        source_lines = files[path]["lines"]
        source_text = "\n".join(source_lines.get(number, "")
                                for number in range(1, max(source_lines, default=0) + 1))
        sdk_imports: dict[str, tuple[str, int]] = {}
        for matcher in IMPORT_BINDING_PATTERNS:
            for match in matcher.finditer(source_text):
                binding, package = match.groups()
                if package.startswith((".", "/", "http:")):
                    continue
                provider = _external_service_key(package.split("/")[0].split(".")[0])
                if provider in NON_SERVICE_SDKS:
                    continue
                import_line = source_text.count("\n", 0, match.start()) + 1
                sdk_imports[binding] = (package, import_line)
        for matcher in (PY_FROM_BINDING_PATTERN, PY_MODULE_BINDING_PATTERN):
            for match in matcher.finditer(source_text):
                module = match.group("module")
                binding = (match.groupdict().get("alias") or match.groupdict().get("name")
                           or module.split(".", 1)[0])
                provider = _external_service_key(module.split(".", 1)[0])
                if provider not in NON_SERVICE_SDKS:
                    import_line = source_text.count("\n", 0, match.start()) + 1
                    sdk_imports[binding] = (module, import_line)

        for binding, (package, import_line) in sdk_imports.items():
            escaped = re.escape(binding)
            assignment = re.compile(
                rf"(?:\b(?:const|let|var)\s+|^\s*)(?P<instance>[A-Za-z_$][\w$]*)\s*=\s*"
                rf"(?:new\s+)?{escaped}(?:\s*\.\s*[A-Za-z_$][\w$]*)*\s*\(", re.M)
            init = assignment.search(source_text)
            if not init:
                continue
            instance = init.group("instance")
            call = re.search(rf"\b{re.escape(instance)}(?:\s*\.\s*[A-Za-z_$][\w$]*)+\s*\(", source_text)
            if not call:
                continue
            key = _external_service_key(package)
            if key in NON_SERVICE_SDKS:
                continue
            node = external_nodes.setdefault(key, {"id": f"external:{key}", "label": _provider_label(key),
                "type": "external", "parent_id": None, "path": None, "hostnames": [], "packages": [],
                "responsibility": "External service client constructed and used in indexed source.",
                "symbols": [], "routes": [], "citations": [], "expandable": False})
            node["label"] = f"{_provider_label(key)} {_integration_role(path, package)}"
            node["integration_role"] = _integration_role(path, package)
            node["responsibility"] = "Third-party SDK client is configured and invoked by indexed source."
            if package not in node["packages"]:
                node["packages"].append(package)
            init_line = source_text.count("\n", 0, init.start()) + 1
            call_line = source_text.count("\n", 0, call.start()) + 1
            citations = [_citation(path, import_line, files[path]), _citation(path, init_line, files[path]),
                         _citation(path, call_line, files[path])]
            for cite in citations:
                if cite not in node["citations"]:
                    node["citations"].append(cite)
            sdk_integrations.append((path, key, citations, _integration_role(path, package)))

    browser_citations = []
    browser_files: dict[str, list[dict]] = defaultdict(list)
    # ``layers`` is keyed by the layer name ("frontend"), while node ids
    # use the ``layer:<name>`` prefix. Check the key used by the local map.
    if "frontend" in layers:
        for path in module_paths:
            if metadata[path]["parent_id"] != "layer:frontend":
                continue
            for line_number, line in _line_records(files[path]):
                if BROWSER_CLIENT_PATTERN.search(line):
                    citation = _citation(path, line_number, files[path])
                    if citation not in browser_citations:
                        browser_citations.append(citation)
                    if citation not in browser_files[path]:
                        browser_files[path].append(citation)
                    break
    browser_node = None
    if browser_citations:
        browser_node = {"id": "client:browser", "label": "Client / User Browser", "type": "client", "parent_id": None,
            "path": None, "responsibility": "Browser runtime evidenced by the indexed frontend routing or DOM APIs.",
            "symbols": [], "routes": [], "citations": browser_citations[:8], "expandable": False}

    nodes.extend(layers.values())
    nodes.extend(database_nodes.values())
    nodes.extend(external_nodes.values())
    nodes.extend(technology_nodes.values())
    if browser_node:
        nodes.append(browser_node)
    node_by_path = {path: metadata[path]["id"] for path in module_paths}
    node_by_id = {node["id"]: node for node in nodes}
    edge_map: dict[tuple, dict] = {}

    if browser_node:
        for path, citations in browser_files.items():
            _add_edge(edge_map, browser_node["id"], node_by_path[path], "renders_in", citations)

    # Resolve local imports, route mounts, and explicit endpoint calls.
    route_rows: list[dict] = []
    router_bindings: dict[str, str] = {}
    mounts: dict[str, list[str]] = defaultdict(list)
    imported_by_file: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    api_calls: list[tuple[str, str | None, str, int]] = []
    model_refs: list[tuple[str, str, int]] = []
    external_usage: list[tuple[str, str, int]] = []
    for path in module_paths:
        for line_number, line in _line_records(files[path]):
            for matcher in IMPORT_BINDING_PATTERNS:
                for binding, specifier in matcher.findall(line):
                    target = _resolve_import(path, specifier, module_path_set)
                    if target:
                        router_bindings[binding] = target
                        imported_by_file[path].append((target, specifier, line_number))
            for matcher in IMPORT_PATTERNS:
                for specifier in matcher.findall(line):
                    target = _resolve_import(path, specifier, module_path_set)
                    if target and all(existing[0] != target or existing[2] != line_number for existing in imported_by_file[path]):
                        imported_by_file[path].append((target, specifier, line_number))
            for prefix, binding in MOUNT_PATTERN.findall(line):
                target = router_bindings.get(binding)
                if target:
                    mounts[target].append(prefix.rstrip("/"))
            for method, route in JS_ROUTE_PATTERN.findall(line) + PY_ROUTE_PATTERN.findall(line):
                route_rows.append({"path": path, "method": method.upper(), "route": route.rstrip("/") or "/",
                                   "line": line_number})
            for model_name in MODEL_REF_PATTERN.findall(line):
                model_refs.append((path, model_name, line_number))
            for host in HOST_PATTERN.findall(line):
                if host.lower() in external_keys_by_host:
                    external_usage.append((path, host.lower(), line_number))

        # API requests are commonly formatted across several lines. Rebuild the
        # source with original line positions so a multiline match keeps an
        # accurate citation line rather than being missed or assigned line 1.
        source_lines = files[path]["lines"]
        source_text = "\n".join(source_lines.get(number, "")
                                for number in range(1, max(source_lines, default=0) + 1))
        for route_match in FLASK_ROUTE_PATTERN.finditer(source_text):
            route = route_match.group(1)
            methods = re.findall(r"['\"](GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)['\"]",
                                 route_match.group("options"), re.I) or ["GET"]
            line_number = source_text.count("\n", 0, route_match.start()) + 1
            for method in methods:
                route_rows.append({"path": path, "method": method.upper(), "route": route.rstrip("/") or "/",
                                   "line": line_number})
        for match in API_CALL_PATTERN.finditer(source_text):
            method = (match.group("method") or "").upper() or None
            url = re.sub(r"\$\{[^}]*\}", "", match.group("url"))
            line_number = source_text.count("\n", 0, match.start()) + 1
            for endpoint in URL_PATH_PATTERN.findall(url):
                api_calls.append((path, method, endpoint.rstrip("/"), line_number))

    for path, imports in imported_by_file.items():
        for target, _specifier, line_number in imports:
            source_kind = metadata[path]["type"]
            target_kind = metadata[target]["type"]
            relation = ("uses_middleware" if target_kind == "middleware" else "uses_service" if target_kind == "service"
                        else "uses_model" if target_kind == "model" else "imports")
            _add_edge(edge_map, node_by_path[path], node_by_path[target], relation,
                      [_citation(path, line_number, files[path])])

    # Store actual route definitions on their source module and link API calls
    # only when method/path evidence matches a declared route (including mounts).
    for route_row in route_rows:
        module = metadata[route_row["path"]]
        prefix_options = mounts.get(route_row["path"], [""])
        full_paths = []
        for prefix in prefix_options:
            if route_row["route"] == "/":
                full_paths.append(prefix or "/")
            else:
                full_paths.append(prefix + route_row["route"])
        route_row["full_paths"] = full_paths
        route_info = {"method": route_row["method"], "path": route_row["route"], "mounted_paths": full_paths,
                      "line": route_row["line"], "citation": _citation(route_row["path"], route_row["line"], files[route_row["path"]])}
        if route_info not in module["routes"]:
            module["routes"].append(route_info)

    for source_path, method, endpoint, line_number in api_calls:
        for route_row in route_rows:
            if method and method != route_row["method"]:
                continue
            def matches_route(full_path: str) -> bool:
                normalized_endpoint = endpoint.rstrip("/") or "/"
                normalized_route = full_path.rstrip("/") or "/"
                return (normalized_endpoint == normalized_route or
                        normalized_route != "/" and normalized_endpoint.endswith(normalized_route) or
                        normalized_endpoint != "/" and normalized_route.endswith(normalized_endpoint))
            if any(matches_route(full) for full in route_row["full_paths"]):
                source_cite = _citation(source_path, line_number, files[source_path])
                route_cite = _citation(route_row["path"], route_row["line"], files[route_row["path"]])
                route_is_flask = any(FLASK_ROUTE_PATTERN.search(line) for _, line in _line_records(files[route_row["path"]]))
                _add_edge(edge_map, node_by_path[source_path], node_by_path[route_row["path"]], "api_call",
                          [source_cite, route_cite], label=(f"HTTP {route_row['method']} {route_row['full_paths'][0]}"
                          if route_is_flask else None))

    # Explicit schema refs (for example Mongoose ObjectId refs) are source-grounded.
    model_ids_by_name: dict[str, str] = {}
    for path in module_paths:
        if metadata[path]["type"] == "model":
            model_ids_by_name[posixpath.basename(path).rsplit(".", 1)[0].lower()] = node_by_path[path]
    for source_path, model_name, line_number in model_refs:
        target_id = model_ids_by_name.get(model_name.lower())
        if target_id:
            _add_edge(edge_map, node_by_path[source_path], target_id, "references_model",
                      [_citation(source_path, line_number, files[source_path])])

    for path in module_paths:
        content = "\n".join(line for _, line in _line_records(files[path]))
        for key, db_node in database_nodes.items():
            patterns = {"mongodb": MONGOOSE_PATTERN, "postgresql": POSTGRES_PATTERN, "redis": REDIS_PATTERN,
                        "qdrant": QDRANT_PATTERN, "configured sql database": SQL_PATTERN}
            pattern = patterns[key]
            match = pattern.search(content)
            if match:
                line_number = next((number for number, line in _line_records(files[path]) if pattern.search(line)), 1)
                _add_edge(edge_map, node_by_path[path], db_node["id"], "connects_to",
                          [_citation(path, line_number, files[path])])
    for source_path, host, line_number in external_usage:
        target = external_nodes[external_keys_by_host[host]]
        browser_checkout = host.split(".", 1)[0] == "checkout" and metadata[source_path]["parent_id"] == "layer:frontend"
        _add_edge(edge_map, node_by_path[source_path], target["id"], "calls_external",
                  [_citation(source_path, line_number, files[source_path])],
                  label="Browser checkout" if browser_checkout else "External API")
    for source_path, key, citations, integration_role in sdk_integrations:
        relationship_label = ("Create & verify payment" if integration_role == "Payment Gateway" else
                              "Email delivery" if integration_role == "Email Service" else
                              "AI requests" if integration_role == "AI API" else "External API")
        _add_edge(edge_map, node_by_path[source_path], external_nodes[key]["id"], "calls_external",
                  citations, label=relationship_label)

    # Connect verified dependency declarations to the application layer they serve.
    for tech in technology_nodes.values():
        package = tech["path"]
        package_layer = tech.get("layer_name") or _layer(package, "configuration", files[package])
        layer_id = f"layer:{package_layer}"
        if layer_id in node_by_id:
            _add_edge(edge_map, layer_id, tech["id"], "implemented_with", tech["citations"][:1])
    for layer_type, layer in layers.items():
        used_technologies = {tech["label"] for tech in technology_nodes.values()
                             if (tech.get("layer_name") or _layer(tech["path"], "configuration", files[tech["path"]])) == layer_type}
        if layer_type == "backend" and "Flask" in used_technologies:
            layer["label"] = "Flask Application"
        elif layer_type == "backend" and "Streamlit" in used_technologies:
            layer["label"] = "Streamlit Application"
        elif layer_type == "backend" and "Express" in used_technologies:
            layer["label"] = "Express Backend API"
        elif layer_type == "frontend" and "HTML / JavaScript UI" in used_technologies and "React" not in used_technologies:
            layer["label"] = "Frontend HTML / JS UI"

    # Flask can serve an indexed HTML template and a JSON API from one app.py.
    # Keep the browser request and response directions explicit in the overview.
    flask_files = [path for path in module_paths if any(FLASK_ROUTE_PATTERN.search(line)
                                                        for _, line in _line_records(files[path]))]
    frontend_files = [path for path in module_paths if metadata[path]["parent_id"] == "layer:frontend"]
    if flask_files and frontend_files:
        for backend_path in flask_files:
            backend_lines = _line_records(files[backend_path])
            render_match = next(((line_number, line) for line_number, line in backend_lines
                                 if "render_template" in line and re.search(r"render_template\s*\(\s*['\"]", line)), None)
            if render_match:
                template_match = re.search(r"render_template\s*\(\s*['\"]([^'\"]+)", render_match[1])
                if template_match:
                    template_path = next((candidate for candidate in frontend_files
                                          if candidate.endswith("/" + template_match.group(1))
                                          or candidate == template_match.group(1)
                                          or candidate.endswith("/templates/" + template_match.group(1))), None)
                    if template_path:
                        _add_edge(edge_map, node_by_path[backend_path], node_by_path[template_path], "renders_in",
                                  [_citation(backend_path, render_match[0], files[backend_path])], label="serves UI")

    # Represent the response direction only when an indexed frontend request
    # matches a backend route and that same route returns JSON. This covers
    # Flask/Express handlers without inferring a response from imports alone.
    json_response_pattern = re.compile(
        r"\b(?:res|response)\s*(?:\.\s*status\s*\([^)]*\)\s*)?\.\s*json\s*\(|\bjsonify\s*\(", re.I)
    for request_edge in list(edge_map.values()):
        if request_edge.get("type") != "api_call":
            continue
        source_path = request_edge["source"].removeprefix("file:")
        route_path = request_edge["target"].removeprefix("file:")
        if (source_path not in metadata or route_path not in metadata
                or metadata[source_path]["parent_id"] != "layer:frontend"
                or metadata[route_path]["parent_id"] != "layer:backend"):
            continue
        response_line = next((line_number for line_number, line in _line_records(files[route_path])
                              if json_response_pattern.search(line)), None)
        if response_line is None:
            continue
        evidence = [_citation(route_path, response_line, files[route_path])]
        evidence.extend(request_edge.get("citations", [])[:1])
        _add_edge(edge_map, "layer:backend", "layer:frontend", "api_call", evidence,
                  label="JSON response")

    _add_ml_pipeline(module_paths, files, metadata, nodes, edge_map, repository_inventory)
    _add_ml_development_flow(module_paths, files, nodes, edge_map, repository_inventory)
    _add_streamlit_user_flow(nodes, edge_map)
    if any(node.get("architecture_component") for node in nodes) and "backend" in layers and layers["backend"]["label"] == "Backend application":
        layers["backend"]["label"] = "ML Inference Application"

    # Register all precise line citations created by deterministic workflow
    # extraction, in addition to the original indexed chunk ranges.
    for item in [*nodes, *edge_map.values()]:
        for citation in item.get("citations", []):
            key = (citation["file_path"], citation["start_line"], citation["end_line"])
            source_records.setdefault(key, {"file_path": key[0], "start_line": key[1], "end_line": key[2]})

    # Every evidence reference is deduplicated and range-checked before output.
    sources = sorted(source_records.values(), key=lambda item: (item["file_path"], item["start_line"], item["end_line"]))
    graph = {"version": 1, "repository": {"id": repository_id, "name": repository_name},
             "nodes": nodes, "edges": list(edge_map.values()), "sources": sources,
             "metadata": {"indexed_source_files": len(paths), "represented_source_files": len(module_paths),
                          "repository_file_inventory_available": repository_inventory is not None,
                          "repository_file_inventory_count": len(repository_inventory or []),
                          "omitted_source_files": omitted_count, "relationship_count": len(edge_map),
                          "generated_by": "deterministic_indexed_source_analysis"}}
    return validate_architecture_graph(graph)


def cached_architecture_graph(repository_name: str, repository_id: int, generation: str | None,
                              points: Iterable[Any], *, commit_sha: str | None = None,
                              interpreter=None) -> dict:
    """Cache graph interpretation per repository, commit, and graph schema version."""
    key = (repository_id, generation, commit_sha, ARCHITECTURE_CACHE_SCHEMA)
    with _GRAPH_CACHE_LOCK:
        cached = _GRAPH_CACHE.get(key)
        if cached is not None:
            _GRAPH_CACHE.move_to_end(key)
            return cached
    point_list = list(points)
    graph = build_architecture_graph(repository_name, repository_id, point_list)
    if interpreter:
        graph = interpreter(graph, point_list)
    with _GRAPH_CACHE_LOCK:
        _GRAPH_CACHE[key] = graph
        _GRAPH_CACHE.move_to_end(key)
        while len(_GRAPH_CACHE) > 8:
            _GRAPH_CACHE.popitem(last=False)
    return graph
