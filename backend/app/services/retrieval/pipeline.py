"""Pure ranking, deduplication, diversity and pagination helpers for RAG retrieval."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from dataclasses import replace
import math
import posixpath
import re
from typing import Callable, Iterable, Any

from app.services.retrieval.query_analysis import QueryProfile, code_tokens
from app.services.retrieval.workflow_context import AUTH_WORKFLOW_PATH_HIT_LIMITS, AUTH_WORKFLOW_ROLE_ORDER, authentication_workflow_roles

_IMPORT_PATTERNS = (
    re.compile(r"\bfrom\s+['\"]([^'\"]+)['\"]"),
    re.compile(r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(r"^\s*from\s+(\.+[A-Za-z_][\w.]*)\s+import\s", re.M),
    re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s", re.M),
    re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)", re.M),
)
_API_CALL_PATTERN = re.compile(
    r"(?:\b(?:api|client|axios)\s*\.\s*(?P<method>get|post|put|patch|delete)|\bfetch\s*)"
    r"\s*\(\s*(?P<quote>['\"`])(?P<endpoint>.*?)(?P=quote)",
    re.I,
)
_ROUTE_DECLARATION_PATTERN = re.compile(
    r"\b(?:router|app|server)\s*\.\s*(get|post|put|patch|delete|options|head)\s*\(\s*['\"`]([^'\"`]+)",
    re.I,
)
_ROUTE_MOUNT_PATTERN = re.compile(
    r"\b(?:app|server)\s*\.\s*use\s*\(\s*['\"`]([^'\"`]+)['\"`]\s*,\s*([A-Za-z_$][\w$]*)",
    re.I,
)
_ROUTE_IMPORT_BINDING_PATTERNS = (
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(r"\bimport\s+([A-Za-z_$][\w$]*)\s+from\s+['\"]([^'\"]+)['\"]"),
)
_URL_PATH_PATTERN = re.compile(r"/(?:api/)?[A-Za-z0-9_./:{}-]+")
_SOURCE_EXTENSIONS = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".java", ".go", ".rs")


def _resolve_import_path(source_path: str, specifier: str, indexed_paths: set[str]) -> str | None:
    """Resolve a local indexed module reference without assuming a project layout."""
    normalized_index = {path.replace("\\", "/").lower(): path for path in indexed_paths}
    raw = specifier.strip().replace("\\", "/")
    if not raw or (not raw.startswith(".") and "://" in raw):
        return None
    bases = []
    if raw.startswith("."):
        if raw.startswith(("./", "../")):
            bases.append(posixpath.normpath(posixpath.join(posixpath.dirname(source_path), raw)))
        else:
            dots = len(raw) - len(raw.lstrip("."))
            parent = posixpath.dirname(source_path)
            for _ in range(max(0, dots - 1)):
                parent = posixpath.dirname(parent)
            module = raw[dots:].replace(".", "/")
            bases.append(posixpath.normpath(posixpath.join(parent, module)))
    else:
        bases.append(raw.replace(".", "/"))
    candidates = []
    for base in bases:
        candidates.append(base)
        if not posixpath.splitext(base)[1]:
            candidates.extend(base + extension for extension in _SOURCE_EXTENSIONS)
            candidates.extend(posixpath.join(base, "index" + extension) for extension in (".js", ".jsx", ".ts", ".tsx"))
            candidates.extend((base + "/__init__.py", base + ".py"))
    for candidate in candidates:
        indexed = normalized_index.get(posixpath.normpath(candidate).lower())
        if indexed:
            return indexed
    # Resolve common package-style Python imports to either module or package files.
    suffixes = [base.lower() for base in candidates]
    matches = [path for path in indexed_paths if any(path.lower().endswith("/" + suffix) for suffix in suffixes)]
    return sorted(matches, key=lambda path: (len(path), path.lower()))[0] if matches else None


def discover_import_related_hits(seed_hits: Iterable[SearchHit], all_repo_points: Iterable,
                                 ranked_hits: Iterable[SearchHit], max_paths: int = 8) -> list[SearchHit]:
    """Find directly imported local files from ranked seed files and return their best chunks."""
    points = list(all_repo_points)
    ranked = list(ranked_hits)
    indexed_paths = {str(_payload(point).get("file_path", "")) for point in points}
    seed_paths = list(dict.fromkeys(str(hit.payload.get("file_path", "")) for hit in seed_hits))
    contents_by_path: dict[str, list[str]] = {}
    for point in points:
        path = str(_payload(point).get("file_path", ""))
        if path:
            contents_by_path.setdefault(path, []).append(str(_payload(point).get("content", "")))
    related_counts: Counter = Counter()
    called_endpoints: set[tuple[str | None, str]] = set()
    for source_path in seed_paths:
        content = "\n".join(contents_by_path.get(source_path, []))
        specifiers = dict.fromkeys(spec for pattern in _IMPORT_PATTERNS for spec in pattern.findall(content))
        for specifier in specifiers:
            target = _resolve_import_path(source_path, specifier, indexed_paths)
            if target and target != source_path:
                related_counts[target] += 1
        for match in _API_CALL_PATTERN.finditer(content):
            method = match.group("method")
            endpoint = re.sub(r"\$\{[^}]*\}", "", match.group("endpoint"))
            paths = _URL_PATH_PATTERN.findall(endpoint)
            for called_path in paths:
                called_endpoints.add((method.upper() if method else None, called_path.rstrip("/")))
    if called_endpoints:
        # Resolve Express-style router mounts so a frontend request to
        # /api/admin/products can match a route declared as /products.
        router_files: dict[str, str] = {}
        mounts: dict[str, list[str]] = {}
        for source_path, source_chunks in contents_by_path.items():
            source = "\n".join(source_chunks)
            bindings = {}
            for pattern in _ROUTE_IMPORT_BINDING_PATTERNS:
                for variable, specifier in pattern.findall(source):
                    target = _resolve_import_path(source_path, specifier, indexed_paths)
                    if target:
                        bindings[variable] = target
                        router_files[variable] = target
            for prefix, variable in _ROUTE_MOUNT_PATTERN.findall(source):
                target = bindings.get(variable) or router_files.get(variable)
                if target:
                    mounts.setdefault(target, []).append(prefix.rstrip("/"))
        for point in points:
            payload = _payload(point)
            path = str(payload.get("file_path", ""))
            if not path or path in seed_paths:
                continue
            method = str(payload.get("symbol_name", "")).split(" ", 1)[0].upper()
            route = str(payload.get("symbol_name", "")).partition(" ")[2]
            route_matches = _ROUTE_DECLARATION_PATTERN.findall(str(payload.get("content", "")))
            if route_matches:
                for declared_method, declared_route in route_matches:
                    route = declared_route
                    method = declared_method.upper()
                    route_path = route.rstrip("/") or "/"
                    full_routes = [prefix + ("" if route_path == "/" else route_path)
                                   for prefix in mounts.get(path, [""])]
                    if any((called_method is None or called_method == method)
                           and any(called_route == full_route or called_route.endswith(full_route)
                                   or full_route.endswith(called_route)
                                   for full_route in full_routes)
                           for called_method, called_route in called_endpoints):
                        related_counts[path] += 1
                        break
            elif route and any((called_method is None or called_method == method)
                               and (called_route == route.rstrip("/") or called_route.endswith(route.rstrip("/"))
                                    or route.rstrip("/").endswith(called_route))
                               for called_method, called_route in called_endpoints):
                related_counts[path] += 1
    best_by_path: dict[str, SearchHit] = {}
    for hit in ranked:
        path = str(hit.payload.get("file_path", ""))
        if path in related_counts and path not in best_by_path:
            best_by_path[path] = hit
    ordered_paths = sorted(best_by_path, key=lambda path: (-related_counts[path], path.lower()))[:max_paths]
    return [best_by_path[path] for path in ordered_paths]


def discover_source_role_hits(ranked_hits: Iterable[SearchHit], profile: QueryProfile,
                              max_paths: int = 12) -> list[SearchHit]:
    """Reserve relevant implementation roles (routes/models/components) for broad queries.

    The decision is based on generic source metadata and query vocabulary, rather than
    repository-specific filenames.  It complements semantic/lexical ranking when a
    workflow spans files whose identifiers do not repeat the question wording.
    """
    ranked = list(ranked_hits)
    # Use only words the user actually asked about to avoid unrelated routes or
    # models entering through broad retrieval expansions (e.g. an order route in a
    # product-management answer).
    query_terms = set(profile.terms)
    aliases = {"products": "product", "orders": "order", "payments": "payment",
               "users": "user", "reviews": "review", "administrators": "admin",
               "administrator": "admin", "models": "model", "schemas": "schema",
               "components": "component", "routes": "route", "middlewares": "middleware",
               "entrypoints": "entrypoint", "dependencies": "dependency", "imports": "import"}
    domain_terms = query_terms & ({
        "auth", "authentication", "authorization", "jwt", "token", "login", "register",
        "order", "payment", "checkout", "product", "inventory", "user", "review",
        "admin", "creator", "email", "verification", "database", "model", "schema",
        "api", "route", "endpoint", "handler", "architecture", "module", "entrypoint",
    } | set(aliases))
    if not domain_terms and not any(intent in profile.intents for intent in ("architecture", "module")):
        return []
    domain_terms = {aliases.get(term, term) for term in domain_terms}
    roles = set()
    if "api" in profile.intents or "cross_file" in profile.intents or "workflow" in profile.intents:
        roles.update(("route", "middleware", "service", "component"))
    if "architecture" in profile.intents:
        roles.update(("entrypoint", "manifest", "config", "service", "route", "model", "component"))
    if "module" in profile.intents:
        roles.update(("route", "middleware", "service", "model", "utility", "component"))
    if "business_logic" in profile.intents or "database" in profile.intents:
        roles.update(("model", "schema", "route", "component"))
    if "authentication" in profile.intents:
        if "business_logic" in profile.intents:
            roles.add("middleware")
            if set(profile.terms) & {"jwt", "token"}:
                roles.add("utility")
        else:
            roles.update(("middleware", "utility", "context", "guard", "component", "model", "route"))
    if not roles:
        return []

    best_by_path: dict[str, tuple[int, int, int, SearchHit]] = {}
    for hit in ranked:
        payload = hit.payload
        path = str(payload.get("file_path", ""))
        if not path or path.lower().endswith((".md", ".rst", ".txt")):
            continue
        # Preserve case so code_tokens can split PascalCase/camelCase basenames
        # such as AdminDashboard into searchable component-role terms.
        path_tokens = {aliases.get(token, token) for token in code_tokens(path)}
        basename_stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        path_tokens.update(aliases.get(token, token) for token in code_tokens(basename_stem))
        symbol_tokens = set(code_tokens(str(payload.get("symbol_name", ""))))
        content_tokens = set(code_tokens(str(payload.get("content", ""))))
        content_tokens = {aliases.get(token, token) for token in content_tokens}
        symbol_tokens = {aliases.get(token, token) for token in symbol_tokens}
        type_name = str(payload.get("symbol_type", "")).lower()
        role_match = (("route" in roles and (type_name == "route" or "route" in path_tokens or "routes" in path_tokens))
                      or ("model" in roles and (type_name in {"model", "class"} or "models" in path_tokens or "model" in path_tokens))
                      or ("schema" in roles and "schema" in path_tokens)
                      or ("component" in roles and ("component" in path_tokens or path.lower().endswith((".jsx", ".tsx", ".vue", ".svelte"))))
                      or ("middleware" in roles and "middleware" in path_tokens)
                      or ("service" in roles and "services" in path_tokens)
                      or ("utility" in roles and "utils" in path_tokens)
                      or ("context" in roles and "context" in path_tokens)
                      or ("entrypoint" in roles and ("index" in path_tokens or "main" in path_tokens or "app" in path_tokens or "server" in path_tokens))
                      or ("manifest" in roles and path.lower().rsplit("/", 1)[-1] in {"package.json", "pyproject.toml", "requirements.txt", "go.mod", "cargo.toml", "pom.xml"})
                      or ("config" in roles and ("config" in path_tokens or "settings" in path_tokens or path.lower().rsplit("/", 1)[-1].startswith(".env.example")))
                      or ("guard" in roles and any(term in path_tokens for term in ("guard", "route"))))
        if not role_match:
            continue
        overlap = len(domain_terms & (path_tokens | symbol_tokens | content_tokens))
        role_only_architecture_evidence = ("architecture" in profile.intents and any(
            role in roles for role in ("entrypoint", "manifest", "config")) and role_match)
        if overlap == 0 and not role_only_architecture_evidence:
            continue
        path_overlap = len(domain_terms & (path_tokens | symbol_tokens))
        if "database" in profile.intents and "business_logic" not in profile.intents:
            role_priority = 0 if ("models" in path_tokens or "model" in path_tokens or type_name in {"model", "class"}) else 1
        else:
            component_match = ("component" in roles and path.lower().endswith((".jsx", ".tsx", ".vue", ".svelte")))
            route_match = ("route" in roles and (type_name == "route" or "route" in path_tokens or "routes" in path_tokens))
            model_match = ("models" in path_tokens or "model" in path_tokens or type_name in {"model", "class"})
            if "business_logic" in profile.intents and "frontend" in query_terms:
                role_priority = 0 if component_match else (1 if route_match else (2 if model_match else 3))
            else:
                role_priority = 0 if route_match else (1 if model_match else 2)
        if "architecture" in profile.intents:
            role_priority = 0 if ("entrypoint" in roles and ("index" in path_tokens or "main" in path_tokens or "app" in path_tokens or "server" in path_tokens)) else (
                1 if ("manifest" in roles and path.lower().rsplit("/", 1)[-1] in {"package.json", "pyproject.toml", "requirements.txt", "go.mod", "cargo.toml", "pom.xml"}) else 2)
        if "business_logic" in profile.intents and "frontend" in query_terms:
            # A component's high-ranked introductory chunk can be semantically
            # similar while omitting the actual workflow action. Prefer the
            # chunk that performs a relevant mutation so the later per-file
            # workflow allocation can reserve executable evidence.
            expanded_path_overlap = len(set(profile.expanded_terms) & path_tokens)
            mutation_score = 0
            if component_match:
                methods = {match.group("method").lower()
                           for match in _API_CALL_PATTERN.finditer(str(payload.get("content", "")))}
                business_requested = domain_terms & {"order", "payment", "product", "inventory"}
                content_matches_domain = bool(business_requested & content_tokens)
                if content_matches_domain:
                    mutation_score += 4 * len(methods & {"post", "put", "patch", "delete"})
                    mutation_score -= 2 * ("get" in methods and not methods & {"post", "put", "patch", "delete"})
            candidate = (role_priority, -(path_overlap + 3 * expanded_path_overlap + mutation_score), -overlap, hit)
        else:
            candidate = (-path_overlap, role_priority, -overlap, hit)
        previous = best_by_path.get(path)
        if previous is None or candidate[:3] < previous[:3]:
            best_by_path[path] = candidate
    selected = sorted(best_by_path.values(), key=lambda item: (item[0], item[1], item[2], item[3].rank))
    return [item[3] for item in selected[:max_paths]]


def discover_model_coverage_hits(ranked_hits: Iterable[SearchHit], profile: QueryProfile,
                                 max_paths: int) -> list[SearchHit]:
    """Return one relevant chunk per model file for repository-wide model questions."""
    best: dict[str, tuple[int, SearchHit]] = {}
    requested = set(profile.terms)
    aliases = {"users": "user", "products": "product", "orders": "order",
               "reviews": "review", "payments": "payment", "models": "model"}
    requested = {aliases.get(term, term) for term in requested}
    requested_entities = requested & {"user", "product", "order", "review", "payment", "category", "token"}
    for hit in ranked_hits:
        payload = hit.payload
        path = str(payload.get("file_path", ""))
        normalized = path.replace("\\", "/").lower()
        if "/models/" not in normalized and str(payload.get("symbol_type", "")).lower() not in {"model", "class"}:
            continue
        path_tokens = {aliases.get(term, term) for term in code_tokens(path)}
        symbol_tokens = {aliases.get(term, term) for term in code_tokens(str(payload.get("symbol_name", "")))}
        matches = len((requested_entities or requested) & (path_tokens | symbol_tokens))
        if requested_entities and matches == 0:
            continue
        previous = best.get(path)
        if previous is None or (matches, -hit.rank) > (previous[0], -previous[1].rank):
            best[path] = (matches, hit)
    ordered = sorted(best.values(), key=lambda pair: (-pair[0], pair[1].rank, str(pair[1].payload.get("file_path", ""))))
    return [hit for _, hit in ordered[:max_paths]]


_DATABASE_CONNECT_PATTERN = re.compile(
    r"(?:\b(?:mongoose\.connect|create_engine|MongoClient|QdrantClient|Redis|SessionLocal)\b|\bDATABASE_URL\b)",
    re.I,
)


def discover_architecture_hits(ranked_hits: Iterable[SearchHit], max_paths: int = 8) -> list[SearchHit]:
    """Choose bounded evidence for architecture roles across arbitrary repo layouts."""
    ranked = list(ranked_hits)
    categories: dict[str, list[SearchHit]] = {key: [] for key in ("entrypoint", "configuration", "database", "routes", "services")}
    for hit in ranked:
        payload = hit.payload
        path = str(payload.get("file_path", "")).replace("\\", "/")
        lower = path.lower()
        basename = lower.rsplit("/", 1)[-1]
        content = str(payload.get("content", ""))
        if basename in {"index.js", "index.ts", "index.jsx", "index.tsx", "main.py", "main.js", "main.ts", "main.jsx", "main.tsx", "app.py", "app.js", "app.ts", "app.jsx", "app.tsx", "server.js", "server.ts", "manage.py", "main.go", "main.rs"}:
            categories["entrypoint"].append(hit)
        if basename in {"package.json", "pyproject.toml", "requirements.txt", "go.mod", "cargo.toml", "pom.xml"} or "/config/" in lower or basename in {"config.js", "config.ts", "settings.py", ".env.example"}:
            categories["configuration"].append(hit)
        if _DATABASE_CONNECT_PATTERN.search(content):
            categories["database"].append(hit)
        if str(payload.get("symbol_type", "")).lower() == "route" or "/routes/" in lower:
            categories["routes"].append(hit)
        if "/services/" in lower or str(payload.get("symbol_type", "")).lower() == "service":
            categories["services"].append(hit)
    selected: list[SearchHit] = []
    used_paths: set[str] = set()
    # One best chunk for each verified architecture role before adding alternatives.
    for role, category in categories.items():
        quota = 2 if role in {"entrypoint", "routes"} else 1
        added = 0
        for candidate in category:
            path = str(candidate.payload.get("file_path", ""))
            if path in used_paths:
                continue
            if role == "entrypoint" and any(path.split("/", 1)[0] == old.split("/", 1)[0]
                                             for old in used_paths if old):
                continue
            selected.append(candidate)
            used_paths.add(path)
            added += 1
            if added >= quota:
                break
    if len(selected) < max_paths:
        selected.extend(hit for hit in ranked if str(hit.payload.get("file_path", "")) not in used_paths)
    return selected[:max_paths]


def prioritize_workflow_roles(role_hits: Iterable[SearchHit], profile: QueryProfile,
                              max_hits: int = 8) -> list[SearchHit]:
    """Reserve a compact UI→route→model evidence chain for frontend workflows."""
    hits = list(role_hits)
    if "frontend" not in set(profile.terms):
        return hits[:max_hits]
    frontend, routes, models, middleware, utilities, other = [], [], [], [], [], []
    requested = set(profile.terms)
    expanded = set(profile.expanded_terms)
    aliases = {"products":"product", "orders":"order", "payments":"payment", "users":"user",
               "administrators":"admin", "administrator":"admin", "reviews":"review"}
    requested = {aliases.get(term, term) for term in requested}
    for hit in hits:
        path = str(hit.payload.get("file_path", ""))
        normalized = path.replace("\\", "/").lower()
        path_terms = {aliases.get(term, term) for term in code_tokens(path)}
        basename_stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        path_terms.update(aliases.get(term, term) for term in code_tokens(basename_stem))
        content_terms = {aliases.get(term, term) for term in code_tokens(str(hit.payload.get("content", "")))}
        evidence_score = 3 * len(requested & path_terms) + len(requested & content_terms)
        if normalized.endswith((".jsx", ".tsx", ".vue", ".svelte")):
            content_relevance = len(requested & content_terms)
            if "business_logic" in profile.intents and content_relevance == 0:
                continue
            business_terms = requested & {"order", "payment", "checkout", "product", "inventory"}
            if "business_logic" in profile.intents and business_terms and not (business_terms & content_terms):
                continue
            stem = normalized.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if "/auth/" in normalized and stem.endswith("route") and not (requested & {"guard", "guards", "protected", "authorization"}):
                continue
            score = evidence_score + 4 * len(expanded & path_terms)
            # When the user names Checkout explicitly, prefer that component
            # over a different order-related page (such as an order history)
            # even if the latter has a denser lexical match.
            if "checkout" in requested and "checkout" in path_terms:
                score += 24
            if business_terms & {"order", "payment"}:
                if any(match.group("method").lower() == "post" for match in _API_CALL_PATTERN.finditer(str(hit.payload.get("content", "")))):
                    score += 16
            if "admin" in requested and "admin" in path_terms:
                score += 8
            if "dashboard" in path_terms and "admin" in requested:
                score += 4
            frontend.append((score, hit))
        elif "/routes/" in normalized or str(hit.payload.get("symbol_type", "")).lower() == "route":
            route_path_score = 10 * len(requested & path_terms) + len(requested & content_terms)
            routes.append((route_path_score, hit))
        elif "/models/" in normalized or str(hit.payload.get("symbol_type", "")).lower() in {"model", "class"}:
            model_path_score = 10 * len(requested & path_terms) + len(requested & content_terms)
            models.append((model_path_score, hit))
        elif "/middleware/" in normalized:
            middleware.append((evidence_score, hit))
        elif "/utils/" in normalized or "/utilities/" in normalized:
            utilities.append((evidence_score, hit))
        else:
            other.append(hit)
    frontend.sort(key=lambda pair: (-pair[0], pair[1].rank))
    routes.sort(key=lambda pair: (-pair[0], pair[1].rank))
    def model_priority(pair):
        path = str(pair[1].payload.get("file_path", ""))
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        path_terms = {aliases.get(term, term) for term in code_tokens(path) + code_tokens(stem)}
        primary_order = 5 if "order" in requested and "order" in path_terms else 0
        return (-(pair[0] + primary_order), pair[1].rank)
    models.sort(key=model_priority)
    middleware.sort(key=lambda pair: (-pair[0], pair[1].rank))
    utilities.sort(key=lambda pair: (-pair[0], pair[1].rank))

    ordered: list[SearchHit] = []
    def add_group(group, quota):
        added = 0
        for item in group:
            if added >= quota:
                break
            hit = item[1] if isinstance(item, tuple) else item
            path = str(hit.payload.get("file_path", ""))
            if hit.id in {existing.id for existing in ordered} or path in {str(existing.payload.get("file_path", "")) for existing in ordered}:
                continue
            ordered.append(hit)
            added += 1
    # The frontend seed is one file. Then reserve the backend endpoints and the
    # primary persistence/security sources requested by the question.
    add_group(frontend, 1)
    add_group(routes, 2)
    add_group(models, 1)
    if requested & {"auth", "authentication", "authorization", "middleware", "token", "jwt"}:
        add_group(middleware, 1)
        add_group(utilities, 1)
    add_group(other, max_hits)
    return ordered[:max_hits]

@dataclass(frozen=True)
class SearchHit:
    point: Any
    score: float
    rank: int
    @property
    def payload(self) -> dict:
        return self.point.payload
    @property
    def id(self):
        return self.point.id

def scroll_all_pages(fetch_page: Callable[[Any], tuple[list, Any]], page_size: int = 512, max_pages: int = 100000) -> list:
    """Follow Qdrant scroll cursors until exhaustion; defend against a repeated cursor."""
    points, offset, seen = [], None, set()
    for _ in range(max_pages):
        batch, next_offset = fetch_page(offset)
        points.extend(batch)
        if next_offset is None or next_offset in seen or next_offset == offset:
            break
        seen.add(next_offset)
        offset = next_offset
    return points

def _payload(point) -> dict:
    return point.payload if hasattr(point, "payload") else point.get("payload", {})

def _point_id(point):
    return getattr(point, "id", None) if hasattr(point, "id") else point.get("id")

def filter_repository_points(points: Iterable, repository_id: int, generation: str | None = None) -> list:
    """Locally enforce repository/generation isolation even when a vector client misbehaves."""
    accepted = []
    for point in points:
        payload = _payload(point)
        if payload.get("repository_id") != repository_id:
            continue
        point_generation = payload.get("index_generation")
        if generation is None and point_generation:
            continue
        if generation is not None and point_generation != generation:
            continue
        accepted.append(point)
    return accepted

def _content_tokens(payload: dict) -> Counter:
    return Counter(code_tokens(" ".join(str(payload.get(k, "")) for k in ("content", "symbol_name", "file_path", "language"))))

def _exact_match_strength(payload: dict, profile: QueryProfile) -> float:
    path = str(payload.get("file_path", "")).lower().replace("\\", "/")
    basename = path.rsplit("/", 1)[-1]
    symbol = str(payload.get("symbol_name", "")).lower()
    content = str(payload.get("content", ""))
    content_tokens = set(code_tokens(content))
    strength = 0.0
    for requested_path in profile.file_paths:
        req = requested_path.lower().replace("\\", "/")
        if path == req or path.endswith("/" + req): strength += 4.0
        elif basename == req.rsplit("/", 1)[-1]: strength += 3.5
        elif req.rsplit("/", 1)[-1] in path: strength += 1.2
    for identifier in profile.identifiers:
        ident = identifier.lower()
        if ident == symbol: strength += 4.5
        elif ident in {x.lower() for x in re.findall(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", str(payload.get("content", "")))}: strength += 3.0
        elif ident in content_tokens: strength += 1.0
    for endpoint in profile.endpoints:
        method, _, route_path = endpoint.partition(" ")
        if endpoint.lower() in content.lower() or (route_path and route_path.lower() in content.lower()
                and (method.lower() not in {"get", "post", "put", "patch", "delete"} or method.lower() in content.lower())):
            strength += 4.0
    return strength

def lexical_rank(points: Iterable, profile: QueryProfile) -> list:
    docs = list(points)
    if not docs:
        return []
    token_counts = [_content_tokens(_payload(p)) for p in docs]
    df = Counter()
    for counts in token_counts:
        df.update(counts.keys())
    avg_len = max(1.0, sum(sum(x.values()) for x in token_counts) / len(token_counts))
    n = len(docs)
    query_weights = {term:1.0 for term in profile.terms}
    query_weights.update({term:0.4 for term in profile.expanded_terms if term not in query_weights})
    scored = []
    for point, counts in zip(docs, token_counts):
        length = sum(counts.values())
        bm25 = 0.0
        for term, query_weight in query_weights.items():
            tf = counts.get(term, 0)
            if not tf:
                continue
            idf = math.log(1.0 + (n - df[term] + 0.5) / (df[term] + 0.5))
            bm25 += query_weight * idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * length / avg_len))
        payload = _payload(point)
        exact = _exact_match_strength(payload, profile)
        path = str(payload.get("file_path", "")).lower().replace("\\", "/")
        is_documentation = path.endswith((".md", ".rst", ".txt"))
        if is_documentation and "documentation" not in profile.intents:
            bm25 *= 0.58
            exact *= 0.72
        elif is_documentation and "documentation" in profile.intents:
            bm25 *= 1.25
        symbol_type = str(payload.get("symbol_type", "")).lower()
        metadata_boost = 0.0
        if "api" in profile.intents and symbol_type == "route": metadata_boost += 6.0
        elif "api" in profile.intents and "/routes/" in path: metadata_boost += 3.0
        if "database" in profile.intents and (symbol_type in {"class", "sql_statement", "model"} or "/models/" in path):
            metadata_boost += 1.5
        if "symbol" in profile.intents and str(payload.get("symbol_name", "")).lower() in {x.lower() for x in profile.identifiers}:
            metadata_boost += 1.0
        scored.append((point, bm25 + exact * 2.0 + metadata_boost, exact))
    scored.sort(key=lambda x: (-x[1], str(_payload(x[0]).get("file_path", "")), int(_payload(x[0]).get("start_line", 0)), str(_point_id(x[0]))))
    return [x[0] for x in scored]

def merge_rankings(dense: Iterable, lexical: Iterable, profile: QueryProfile, rrf_k: int = 60) -> list[SearchHit]:
    points: dict[Any, Any] = {}
    scores: Counter = Counter()
    exact: dict[Any, float] = {}
    for rank, point in enumerate(dense, 1):
        key = _point_id(point)
        if key is None: key = (str(_payload(point).get("file_path", "")), _payload(point).get("content_hash"))
        points[key] = point
        scores[key] += 1.0 / (rrf_k + rank)
    for rank, point in enumerate(lexical, 1):
        key = _point_id(point)
        if key is None: key = (str(_payload(point).get("file_path", "")), _payload(point).get("content_hash"))
        points[key] = point
        scores[key] += 1.0 / (rrf_k + rank)
        exact[key] = _exact_match_strength(_payload(point), profile)
    for key, point in points.items():
        scores[key] += min(exact.get(key, _exact_match_strength(_payload(point), profile)), 6.0) * 0.008
        path = str(_payload(point).get("file_path", "")).lower().replace("\\", "/")
        is_documentation = path.endswith((".md", ".rst", ".txt"))
        code_question = any(intent in profile.intents for intent in ("api", "database", "workflow", "cross_file", "frontend_backend", "debugging", "file", "symbol"))
        if is_documentation and "documentation" not in profile.intents and code_question:
            scores[key] -= 0.025
        elif is_documentation and "documentation" in profile.intents:
            scores[key] += 0.008
        payload = _payload(point)
        if "api" in profile.intents and str(payload.get("symbol_type", "")).lower() == "route":
            scores[key] += 0.015
        elif "api" in profile.intents and "/routes/" in path:
            scores[key] += 0.01
    ordered = sorted(points, key=lambda k: (-scores[k], str(_payload(points[k]).get("file_path", "")), int(_payload(points[k]).get("start_line", 0)), str(k)))
    return [SearchHit(points[key], float(scores[key]), rank) for rank, key in enumerate(ordered, 1)]

def _overlap_ratio(a: dict, b: dict) -> float:
    start = max(int(a.get("start_line", 0)), int(b.get("start_line", 0)))
    end = min(int(a.get("end_line", 0)), int(b.get("end_line", 0)))
    overlap = max(0, end - start + 1)
    shorter = min(int(a.get("end_line", 0)) - int(a.get("start_line", 0)) + 1,
                  int(b.get("end_line", 0)) - int(b.get("start_line", 0)) + 1)
    return overlap / shorter if shorter > 0 else 0.0

def select_diverse(hits: Iterable[SearchHit], profile: QueryProfile, limit: int | None = None) -> list[SearchHit]:
    max_results = limit or profile.top_k
    selected: list[SearchHit] = []
    file_counts: Counter = Counter()
    documentation_count = 0
    code_focused = any(intent in profile.intents for intent in ("api", "database", "workflow", "cross_file", "frontend_backend", "debugging", "file", "symbol", "architecture"))
    seen_chunks, file_ranges = set(), {}
    for hit in hits:
        payload = hit.payload
        path = str(payload.get("file_path", ""))
        is_documentation = path.lower().endswith((".md", ".rst", ".txt"))
        # Keep documentation useful while reserving room for implementation
        # evidence, including in README-oriented generator requests.
        documentation_cap = 2 if "documentation" in profile.intents else 1
        if is_documentation and documentation_count >= documentation_cap:
            continue
        # Content-identical chunks in different files still need separate citations.
        digest = (path, payload.get("content_hash") or _point_id(hit.point))
        if digest in seen_chunks:
            continue
        if file_counts[path] >= profile.max_per_file:
            continue
        if any(_overlap_ratio(payload, other) >= 0.82 for other in file_ranges.get(path, [])):
            continue
        selected.append(hit)
        seen_chunks.add(digest)
        file_counts[path] += 1
        file_ranges.setdefault(path, []).append(payload)
        if is_documentation:
            documentation_count += 1
        if len(selected) >= max_results:
            break
    return selected

def retrieve_results(dense: Iterable, all_repo_points: Iterable, profile: QueryProfile, limit: int | None = None) -> list[SearchHit]:
    repo_points = list(all_repo_points)
    lexical = lexical_rank(repo_points, profile)
    merged = merge_rankings(dense, lexical, profile)
    if "authentication_workflow" not in profile.intents:
        result_limit = limit or profile.top_k
        if not any(intent in profile.intents for intent in ("api", "workflow", "cross_file", "frontend_backend", "business_logic", "database", "architecture", "module")):
            return select_diverse(merged, profile, result_limit)
        seeds = select_diverse(merged, profile, min(3, result_limit))
        related = discover_import_related_hits(seeds, repo_points, merged)
        transitive_related = discover_import_related_hits(related, repo_points, merged) if related else []
        # This is a candidate pool, not the returned context limit. Keep enough
        # distinct source files for UI candidates not to crowd out routes/models;
        # the configured result/context limits below still bound the final answer.
        roles = discover_source_role_hits(merged, profile, max_paths=128)
        model_question = ("database" in profile.intents and "business_logic" not in profile.intents
                          and bool({"model", "models", "schema", "schemas", "relationship", "relationships"}
                                   & set(profile.terms)))
        model_coverage = discover_model_coverage_hits(merged, profile, result_limit) if model_question else []
        architecture_coverage = (discover_architecture_hits(merged, result_limit)
                                 if "architecture" in profile.intents else [])
        role_input = roles + seeds if "frontend" in profile.terms else seeds + roles
        role_paths = prioritize_workflow_roles(role_input, profile, max_hits=result_limit)
        # Reserve bounded role slots before re-filling the list with semantically
        # similar chunks. Model questions get one chunk per relevant model; workflow
        # questions get related UI, route, middleware and model evidence.
        reserved = model_coverage + architecture_coverage + role_paths + related + transitive_related + seeds
        reserved_ids = {item.id for item in reserved}
        candidates = reserved + [hit for hit in merged if hit.id not in reserved_ids]
        return select_diverse(candidates, profile, result_limit)

    # Reserve early context slots for each indexed stage of the auth flow. Generic
    # documentation and semantically similar chunks can otherwise crowd out the
    # middleware, token utilities, UI state, and route guards needed to explain it.
    workflow_hits: list[SearchHit] = []
    used_ids: set[Any] = set()
    for role in AUTH_WORKFLOW_ROLE_ORDER:
        role_hits = [hit for hit in merged if role in authentication_workflow_roles(hit.payload.get("file_path", ""))]
        paths = dict.fromkeys(str(hit.payload.get("file_path", "")) for hit in role_hits)
        for path in paths:
            path_hits = [hit for hit in role_hits if str(hit.payload.get("file_path", "")) == path]
            quota = AUTH_WORKFLOW_PATH_HIT_LIMITS[role]
            added = 0
            for hit in path_hits:
                if hit.id in used_ids:
                    continue
                workflow_hits.append(hit)
                used_ids.add(hit.id)
                added += 1
                if added >= quota:
                    break

    base_limit = limit or profile.top_k
    workflow_limit = min(28, max(base_limit, 24))
    seeds = select_diverse(merged, profile, min(6, base_limit))
    related = discover_import_related_hits(seeds, repo_points, merged)
    candidates = workflow_hits + related + [hit for hit in merged if hit.id not in used_ids and hit.id not in {item.id for item in related}]
    workflow_profile = replace(profile, max_per_file=max(profile.max_per_file, 4))
    return select_diverse(candidates, workflow_profile, workflow_limit)
