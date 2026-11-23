"""Deterministic query profiling; deliberately makes no LLM call."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re

STOP_WORDS = {"a", "an", "the", "how", "what", "where", "when", "why", "does", "do", "did", "is", "are", "was", "were", "it", "this", "that", "these", "those", "to", "of", "in", "on", "for", "from", "with", "and", "or", "by", "as", "be", "can", "i", "me", "my", "explain", "show", "find", "tell", "about", "please", "using", "used", "use"}
TECH_TERMS = {"function", "method", "class", "component", "api", "endpoint", "route", "middleware", "authentication", "authorization", "jwt", "database", "model", "payment", "order", "frontend", "backend", "workflow", "import", "dependency", "validation", "error", "architecture", "file", "calls", "called"}
CONCEPT_EXPANSIONS = {
    "jwt": ("token", "bearer", "signing", "verification", "claims"),
    "authentication": ("auth", "login", "credentials", "token", "middleware"),
    "authorization": ("permissions", "roles", "access control", "guard"),
    "database": ("model", "schema", "table", "query", "persistence"),
    "model": ("schema", "entity", "table", "fields", "relationship"),
    "endpoint": ("route", "handler", "request", "response", "status"),
    "route": ("endpoint", "handler", "middleware", "request", "response"),
    "api": ("endpoint", "route", "handler", "request", "response"),
    "architecture": ("entrypoint", "module", "service", "dependency", "import"),
    "debug": ("error", "exception", "validation", "logging", "failure"),
    "payment": ("checkout", "transaction", "signature", "order"),
    "order": ("checkout", "cart", "place order", "create order", "order route", "order model"),
    "checkout": ("order", "cart", "payment", "shipping"),
}
DOCUMENTATION_RETRIEVAL_PROFILES = {
    "overview": ("repository purpose, source verified features, project structure, entry points, package manifests, frameworks and technologies", 12),
    "readme": ("README documentation installation setup configuration environment variables usage scripts dependencies supported features and examples", 12),
    "api": ("API route definitions HTTP methods endpoint paths middleware request validation response payloads status codes authentication handlers", 16),
    "architecture": ("architecture frontend entry points backend entry points package manifests configuration environment settings database connection initialization API route mounting service dependencies verified imports and actual module interactions", 16),
    "module": ("module responsibilities files functions classes exports imports callers callees and dependencies", 14),
}


def documentation_retrieval_query(kind: str, repository_label: str = "") -> tuple[str, int]:
    query, limit = DOCUMENTATION_RETRIEVAL_PROFILES.get(kind, DOCUMENTATION_RETRIEVAL_PROFILES["overview"])
    return f"{query} {repository_label}".strip(), limit
TOKEN_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*|\d+|#[A-Za-z0-9_-]+")
PATH_RE = re.compile(r"(?:[\w.-]+/)*[\w.-]+\.(?:pyi?|jsx?|mjs|cjs|tsx?|mts|cts|java|c|h|cpp|hpp|cc|go|rs|html|css|sql|json|ya?ml|md)\b", re.I)
ENDPOINT_RE = re.compile(r"(?:(?:GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s+)?/(?:api/)?[A-Za-z0-9_./:{}-]+", re.I)
IDENTIFIER_RE = re.compile(r"\b(?:[A-Za-z_$][\w$]*\.)+[A-Za-z_$][\w$]*\b|\b[A-Za-z_$][\w$]*[A-Z][\w$]*\b")

def code_tokens(value: str) -> list[str]:
    raw = TOKEN_RE.findall(value or "")
    result: list[str] = []
    for item in raw:
        result.append(item.lower())
        # Split camelCase and snake_case to support conceptual matching without losing exact names.
        pieces = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", item).replace("_", " ").split()
        result.extend(piece.lower() for piece in pieces if len(piece) > 1)
    return result

@dataclass(frozen=True)
class QueryProfile:
    query: str
    terms: tuple[str, ...]
    identifiers: tuple[str, ...]
    file_paths: tuple[str, ...]
    endpoints: tuple[str, ...]
    intents: tuple[str, ...]
    complex: bool
    top_k: int
    max_per_file: int
    expanded_terms: tuple[str, ...] = ()

def analyze_query(query: str) -> QueryProfile:
    lowered = query.lower()
    normalized_phrases = re.sub(r"[-_]+", " ", lowered)
    tokens = code_tokens(query)
    terms = tuple(dict.fromkeys(t for t in tokens if len(t) > 2 and t not in STOP_WORDS))
    paths = tuple(dict.fromkeys(x.replace("\\", "/") for x in PATH_RE.findall(query)))
    endpoints = tuple(dict.fromkeys(x.strip(".,;`)'”\"") for x in ENDPOINT_RE.findall(query)))
    identifiers = list(IDENTIFIER_RE.findall(query))
    identifiers.extend(re.findall(r"[`\"]([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)[`\"]", query))
    for path in paths:
        identifiers.append(PurePosixPath(path).name)
    identifiers = tuple(dict.fromkeys(x for x in identifiers if x.lower() not in STOP_WORDS))
    intents = []
    if paths: intents.append("file")
    if identifiers: intents.append("symbol")
    if endpoints or any(x in terms for x in ("endpoint", "route", "api")): intents.append("api")
    workflow_markers = ("workflow", "flow", "end to end", "complete flow", "from the frontend", "across",
                        "multi file", "multiple files", "trace", "from frontend to backend")
    if any(x in normalized_phrases for x in workflow_markers):
        intents.append("workflow")
    auth_terms = {"auth", "authentication", "authorization", "jwt", "token", "login", "signin", "register", "registration", "signup", "password"}
    auth_signals = auth_terms & set(terms)
    if auth_signals: intents.append("authentication")
    auth_flow_markers = ("flow", "workflow", "end to end", "complete", "full", "from", "across", "through", "trace", "process", "verification", "activation")
    other_domain_signals = {"payment", "order", "checkout", "razorpay", "search", "product", "repository"} & set(terms)
    authentication_is_primary = bool(auth_signals) and (not other_domain_signals or len(auth_signals) > len(other_domain_signals))
    if authentication_is_primary and any(marker in normalized_phrases for marker in auth_flow_markers):
        intents.append("authentication_workflow")
    if any(x in terms for x in ("database", "model", "relationship", "schema", "query")): intents.append("database")
    if any(x in terms for x in ("frontend", "react", "component", "browser")) and any(x in terms for x in ("backend", "api", "route", "server")):
        intents.append("frontend_backend")
    if any(x in terms for x in ("payment", "order", "checkout", "razorpay", "product", "inventory")): intents.append("business_logic")
    if any(x in terms for x in ("architecture", "dependency", "project")): intents.append("architecture")
    if "module" in terms: intents.append("module")
    if (any(x in terms for x in ("error", "exception", "fail", "failure", "debug", "troubleshoot", "broken", "issue", "crash"))
            or any(x in normalized_phrases for x in ("why might", "not working", "does not work", "doesn't work", "fails", "failing", "cannot work"))):
        intents.append("debugging")
    if re.search(r"\b(what is|define|meaning of|concept of)\b", normalized_phrases): intents.append("conceptual")
    if any(x in normalized_phrases for x in ("readme", "installation", "install", "setup", "usage", "documentation", "docs")):
        intents.append("documentation")
    if any(x in normalized_phrases for x in ("connect", "calls it makes", "api call", "api calls", "caller", "called by", "used by", "interact", "integration")):
        intents.append("cross_file")
    if "workflow" in intents or "frontend_backend" in intents:
        intents.append("cross_file")
    if "authentication_workflow" in intents and "workflow" not in intents:
        intents.append("workflow")
    expansions: list[str] = []
    for term in terms:
        expansions.extend(CONCEPT_EXPANSIONS.get(term, ()))
    if "api" in intents:
        expansions.extend(("handler", "request", "response", "validation"))
    if "database" in intents:
        expansions.extend(("migration", "foreign key", "relation", "persistence"))
    if "business_logic" in intents:
        expansions.extend(("create", "update", "save", "schema", "model", "handler", "route", "component", "api"))
    if "database" in intents:
        expansions.extend(("user", "product", "order", "review", "payment", "reference", "relationship"))
    if "debugging" in intents:
        expansions.extend(("exception", "stack trace", "error handling", "log", "validation"))
    if "architecture" in intents or "cross_file" in intents:
        expansions.extend(("import", "dependency", "caller", "callee", "service", "entrypoint"))
    if "architecture" in intents:
        expansions.extend(("connect", "mongoose.connect", "create_engine", "MongoClient", "QdrantClient",
                           "configuration", "environment", "router", "mount", "package.json"))
    if "module" in intents:
        expansions.extend(("function", "class", "export", "import", "model", "middleware", "service"))
    if "authentication_workflow" in intents:
        expansions.extend(("email", "verification", "verify", "activation", "active", "password", "compare",
                           "bcrypt", "jwt", "token", "register", "login", "middleware", "context", "route", "user"))
    expanded_terms = tuple(dict.fromkeys(term.lower() for term in expansions if len(term) > 2 and term.lower() not in terms))
    complex_query = len(terms) >= 13 or any(x in intents for x in ("workflow", "cross_file", "frontend_backend", "architecture", "module"))
    top_k = 16 if complex_query else (8 if paths or identifiers else 12)
    return QueryProfile(query, terms, identifiers, paths, endpoints, tuple(dict.fromkeys(intents)), complex_query,
                        top_k, 2 if complex_query else 3, expanded_terms)
