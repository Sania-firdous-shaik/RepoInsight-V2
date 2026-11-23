"""Path roles used to broaden retrieval for explicit multi-file auth questions."""
from __future__ import annotations

from pathlib import PurePosixPath
import re

AUTH_WORKFLOW_ROLE_ORDER = (
    "backend_auth_routes",
    "auth_middleware",
    "jwt_utility",
    "auth_context",
    "login_component",
    "registration_component",
    "route_guard",
    "account_model",
    "email_verification_page",
    "email_sender",
)

AUTH_WORKFLOW_PATH_HIT_LIMITS = {
    "backend_auth_routes": 4,
    "auth_middleware": 2,
    "jwt_utility": 1,
    "auth_context": 2,
    "login_component": 2,
    "registration_component": 3,
    "route_guard": 1,
    "account_model": 1,
    "email_verification_page": 2,
    "email_sender": 1,
}

_GUARD_NAMES = {
    "adminroute", "authguard", "authroute", "authenticatedroute", "creatorroute",
    "privateroute", "protectedroute", "requireauth", "userroute",
}


def authentication_workflow_roles(path: str) -> tuple[str, ...]:
    normalized = str(path or "").replace("\\", "/").lower()
    parts = normalized.split("/")
    stem = PurePosixPath(parts[-1]).stem
    roles: list[str] = []

    if "routes" in parts and (stem in {"auth", "authentication", "user", "users"} or "auth" in stem):
        roles.append("backend_auth_routes")
    if "middleware" in parts and ("auth" in stem or "auth" in normalized):
        roles.append("auth_middleware")
    if "jwt" in stem or ("utils" in parts and ("token" in stem or "auth" in stem)):
        roles.append("jwt_utility")
    if stem in {"authcontext", "authenticationcontext", "authprovider"} or ("context" in parts and "auth" in stem):
        roles.append("auth_context")
    if stem.startswith(("login", "signin")):
        roles.append("login_component")
    if stem.startswith(("register", "registration", "signup")):
        roles.append("registration_component")
    if stem in _GUARD_NAMES or stem.endswith("routeguard"):
        roles.append("route_guard")
    if "models" in parts and stem in {"user", "users", "account"}:
        roles.append("account_model")
    if stem in {"verifyemail", "emailverification", "verification"} or ("verification" in stem and "pages" in parts):
        roles.append("email_verification_page")
    if "utils" in parts and ("email" in stem or "mailer" in stem):
        roles.append("email_sender")
    return tuple(roles)


def authentication_workflow_index_inventory(paths: list[str]) -> dict[str, list[str]]:
    inventory = {role: [] for role in AUTH_WORKFLOW_ROLE_ORDER}
    for path in paths:
        for role in authentication_workflow_roles(path):
            inventory[role].append(path)
    return {role: sorted(set(role_paths)) for role, role_paths in inventory.items()}


def format_authentication_workflow_inventory(inventory: dict[str, list[str]]) -> str:
    labels = {
        "backend_auth_routes": "backend authentication routes",
        "auth_middleware": "authentication middleware",
        "jwt_utility": "JWT/token utility",
        "auth_context": "frontend auth context/provider",
        "login_component": "login/sign-in component",
        "registration_component": "registration/sign-up component",
        "route_guard": "frontend route guards",
        "account_model": "account/user model",
        "email_verification_page": "email-verification page",
        "email_sender": "email-sending utility",
    }
    return "\n".join(
        f"- {labels[role]}: {', '.join(inventory.get(role, [])) if inventory.get(role) else 'no matching path found in the indexed repository'}"
        for role in AUTH_WORKFLOW_ROLE_ORDER
    )


def missing_authentication_workflow_evidence(inventory: dict[str, list[str]], sources: list[dict]) -> list[str]:
    """Describe workflow roles absent from the indexed paths or answer context."""
    labels = {
        "backend_auth_routes": "backend authentication routes",
        "auth_middleware": "authentication middleware",
        "jwt_utility": "JWT/token utility",
        "auth_context": "frontend auth context/provider",
        "login_component": "login/sign-in component",
        "registration_component": "registration/sign-up component",
        "route_guard": "frontend route guards",
        "account_model": "account/user model",
        "email_verification_page": "email-verification page",
        "email_sender": "email-sending utility",
    }
    retrieved_paths = {str(source.get("file_path", "")) for source in sources}
    notes = []
    for role in AUTH_WORKFLOW_ROLE_ORDER:
        indexed_paths = inventory.get(role, [])
        if not indexed_paths:
            notes.append(f"No indexed path matching the expected {labels[role]} role was found.")
        else:
            absent = [path for path in indexed_paths if path not in retrieved_paths]
            if absent:
                notes.append(f"Indexed {labels[role]} path(s) exist but were not retrieved for this answer: {', '.join(absent)}.")
    return notes


def authentication_workflow_stage_requirements(question: str, sources: list[dict]) -> tuple[dict[str, list[list[str]]], list[str]]:
    """Return compact, question-scoped evidence groups for logical auth stages.

    Each inner citation-ID group represents a source file/role that must contribute
    evidence when it is present in the assembled context. Missing stages are
    returned separately so the API can report them explicitly without a model call.
    """
    query = re.sub(r"[-_]+", " ", str(question or "").lower())
    broad_markers = bool(re.search(r"\b(flow|workflow|complete|full|end to end|trace|process)\b", query))
    # A generic business flow (order/payment/product) is not an authentication
    # workflow. Only broaden to every auth stage when auth itself is the topic.
    explicit_authentication_subject = bool(re.search(r"\b(auth|authentication|authorization)\b", query))
    other_workflow_topic = bool(re.search(r"\b(order|orders|payment|payments|checkout|product|products|inventory|cart)\b", query))
    broad = broad_markers and explicit_authentication_subject and not other_workflow_topic
    requested = {
        "registration": broad or bool(re.search(r"\b(register|registration|signup|sign up)\b", query)),
        "email verification and activation": broad or bool(
            re.search(r"\bemail\b.{0,32}\b(verify|verification|verified|activation|activate)\b", query)
            or re.search(r"\b(verify|verification|verified)\b.{0,32}\bemail\b", query)
            or re.search(r"\b(account|user)\s+activation\b", query)),
        "login, password and token": broad or bool(re.search(r"\b(login|password|jwt|token|signin)\b", query)),
        "frontend authentication state": broad or bool(re.search(r"\b(authcontext|auth context|session|authentication state)\b", query)),
        "frontend route guards": broad or bool(re.search(r"\b(guard|guards|protected route|route protection|privateroute|adminroute|creatorroute)\b", query)),
    }
    role_groups = {
        "registration": (("backend_auth_routes",), ("registration_component",)),
        "email verification and activation": (("backend_auth_routes",), ("email_verification_page",), ("account_model",), ("email_sender",)),
        "login, password and token": (("backend_auth_routes",), ("auth_middleware",), ("jwt_utility",), ("login_component",)),
        "frontend authentication state": (("auth_context",),),
        "frontend route guards": (("route_guard",),),
    }
    by_role_path: dict[str, dict[str, list[str]]] = {}
    for source in sources:
        if source.get("source_kind", "source_code") != "source_code":
            continue
        path = str(source.get("file_path", ""))
        for role in authentication_workflow_roles(path):
            by_role_path.setdefault(role, {}).setdefault(path, []).append(str(source.get("citation_id", "")))

    requirements: dict[str, list[list[str]]] = {}
    unavailable: list[str] = []
    for stage, is_requested in requested.items():
        if not is_requested:
            continue
        groups: list[list[str]] = []
        for role_group in role_groups[stage]:
            # A role group may match multiple files (route guards); each available
            # path gets its own required citation group.
            role_files = [by_role_path.get(role, {}) for role in role_group]
            paths = sorted({path for mapping in role_files for path in mapping})
            for path in paths:
                ids = list(dict.fromkeys(cid for mapping in role_files for cid in mapping.get(path, []) if cid))
                if ids:
                    groups.append(ids)
        if groups:
            requirements[stage] = groups
        else:
            unavailable.append(stage)
    return requirements, unavailable


def business_workflow_stage_requirements(question: str, sources: list[dict]) -> tuple[dict[str, list[list[str]]], list[str]]:
    """Require supplied UI, route, and model evidence for explicit order/payment flows."""
    query = re.sub(r"[-_]+", " ", str(question or "").lower())
    workflow = bool(re.search(r"\b(flow|workflow|process|trace|through|end to end)\b", query))
    asks_order = bool(re.search(r"\b(order|orders|checkout|cart|placement|placing)\b", query))
    asks_payment = bool(re.search(r"\b(payment|payments|razorpay|verification|verify)\b", query))
    if not workflow or not (asks_order or asks_payment):
        return {}, []

    normalized_sources = []
    for source in sources:
        if source.get("source_kind", "source_code") != "source_code":
            continue
        path = str(source.get("file_path", "")).replace("\\", "/").lower()
        if path:
            normalized_sources.append((path, str(source.get("citation_id", ""))))

    definitions: list[tuple[str, object, bool]] = []
    explicit_checkout = bool(re.search(r"\bcheckout\b", query))
    explicit_frontend = bool(re.search(r"\b(frontend|front end|client|ui|component)\b", query))
    if asks_order and explicit_frontend:
        def frontend_order(path: str) -> bool:
            component = path.endswith((".jsx", ".tsx", ".vue", ".svelte"))
            frontend = any(part in path.split("/") for part in ("frontend", "client", "web", "src")) or component
            if not component or not frontend:
                return False
            if explicit_checkout:
                return "checkout" in path.rsplit("/", 1)[-1]
            return any(term in path.rsplit("/", 1)[-1] for term in ("checkout", "order", "cart"))
        definitions.append(("frontend order placement", frontend_order, True))
    if asks_order:
        definitions.append(("backend order route", lambda path: "/routes/" in path and re.search(r"/orders?(?:\.|/)", path) is not None, True))
    if asks_payment:
        definitions.append(("backend payment verification", lambda path: "/routes/" in path and re.search(r"/payments?(?:\.|/)", path) is not None, True))
    if asks_order and re.search(r"\b(database|model|models|persistence|saved|stored)\b", query):
        definitions.append(("order database model", lambda path: "/models/" in path and re.search(r"/orders?(?:\.|/)", path) is not None, True))

    requirements: dict[str, list[list[str]]] = {}
    unavailable: list[str] = []
    for stage, matches, requested in definitions:
        paths: dict[str, list[str]] = {}
        for path, citation_id in normalized_sources:
            if citation_id and matches(path):
                paths.setdefault(path, []).append(citation_id)
        groups = [list(dict.fromkeys(ids)) for ids in paths.values()]
        if groups:
            requirements[stage] = groups
        elif requested:
            unavailable.append(stage)
    return requirements, unavailable
