"""Syntax-aware code chunking with lossless coverage for indexed source files."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any

EXT_LANG = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".cjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".mts": "TypeScript", ".cts": "TypeScript", ".java": "Java", ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".go": "Go", ".rs": "Rust",
    ".html": "HTML", ".css": "CSS", ".sql": "SQL", ".json": "JSON", ".ipynb": "Jupyter Notebook",
    ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".ini": "INI", ".cfg": "Configuration",
    ".conf": "Configuration", ".properties": "Properties", ".md": "Markdown",
}
SKIP_DIRS = {".git", "node_modules", "dist", "build", ".next", "venv", ".venv", "__pycache__", ".pytest_cache", ".idea", ".vscode", "vendor", "coverage"}
SKIP_NAMES = {".env", ".env.local", "id_rsa", "id_ed25519", "credentials", "credentials.json", "secrets.json", "service-account.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock"}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".crt", ".cer")
CHUNKER_VERSION = "syntax-coverage-v3"
MAX_SYMBOL_LINES = 240
FALLBACK_LINES = 90

def language_for_path(path: str) -> str | None:
    item = PurePosixPath(path)
    name = item.name.lower()
    if name == "requirements.txt":
        return "Python requirements"
    if name == ".env.example":
        return "Environment example"
    if name in {"dockerfile", "makefile"}:
        return "Build configuration"
    return EXT_LANG.get(item.suffix.lower())

def is_sensitive_file(path: Any) -> bool:
    name = PurePosixPath(str(path)).name.lower()
    return (name.startswith(".env") and name != ".env.example" or name in SKIP_NAMES or name.endswith(SECRET_SUFFIXES)
            or any(term in name for term in ("secret", "credential", "private_key", "service-account")))

_FUNCTION_TYPES = {
    "function_definition", "function_declaration", "generator_function_declaration", "method_definition",
    "function_item", "function_definition", "method_declaration", "constructor_declaration",
    "function_expression", "arrow_function", "function_item", "function_literal", "func_literal",
    "function_declarator", "function_definition",
}
_CLASS_TYPES = {
    "class_definition", "class_declaration", "class_specifier", "struct_item", "struct_declaration",
    "interface_declaration", "trait_item", "impl_item", "enum_declaration", "enum_item",
}
_SQL_TYPES = {"create_table_statement", "create_function_statement", "create_view_statement", "select_statement", "insert_statement", "update_statement", "delete_statement", "alter_table_statement"}
_CSS_TYPES = {"rule_set", "qualified_rule"}
_VARIABLE_TYPES = {"variable_declarator", "lexical_declaration", "variable_declaration", "public_field_definition"}
_ROUTE_RE = re.compile(r"\b(?:app|router|server)\s*\.\s*(get|post|put|patch|delete|options|head|all|use)\s*\(\s*(['\"`])([^'\"`]+)", re.I)
_NAME_RE = re.compile(r"(?:def|class|function|interface|struct|enum|fn|func|const|let|var)\s+([\w$]+)")

def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)

def _field_name(node) -> str | None:
    name = node.child_by_field_name("name") if hasattr(node, "child_by_field_name") else None
    if name is not None:
        return name.text.decode("utf-8", "replace")
    parent = getattr(node, "parent", None)
    while parent is not None and parent.end_byte >= node.end_byte:
        if parent.type == "variable_declarator":
            ident = parent.child_by_field_name("name")
            if ident is not None:
                return ident.text.decode("utf-8", "replace")
        if parent.type in ("decorated_definition", "export_statement"):
            node = parent
        else:
            break
        parent = getattr(parent, "parent", None)
    return None

def _symbol_type(node_type: str, name: str, text: str) -> str:
    if node_type in _CLASS_TYPES:
        if node_type in {"interface_declaration", "trait_item"}:
            return "interface"
        return "class"
    if node_type in _SQL_TYPES:
        return "sql_statement"
    if node_type == "call_expression":
        return "route"
    if name[:1].isupper() and ("jsx" in text.lower() or re.search(r"</?[A-Za-z][^>]*>", text)):
        return "component"
    if node_type in {"method_definition", "method_declaration", "constructor_declaration"}:
        return "method"
    if node_type in _FUNCTION_TYPES or node_type in _VARIABLE_TYPES:
        return "function"
    return "section"

def _syntax_candidates(path: str, text: str, grammar: str) -> list[dict]:
    from tree_sitter_language_pack import get_parser
    tree = get_parser(grammar).parse(text.encode("utf-8"))
    lines = text.splitlines()
    candidates: list[dict] = []
    nodes = list(_walk(tree.root_node))

    def add(node, node_type: str | None = None, forced_name: str | None = None, forced_kind: str | None = None):
        start = node.start_point.row + 1
        end = node.end_point.row + (1 if node.end_point.column else 0)
        end = max(start, end)
        if end - start + 1 > MAX_SYMBOL_LINES:
            return
        raw = text.encode("utf-8")[node.start_byte:node.end_byte].decode("utf-8", "replace").strip("\n")
        if not raw.strip():
            return
        kind_node = node_type or node.type
        name = forced_name or _field_name(node)
        if not name:
            match = _NAME_RE.search(raw)
            name = match.group(1) if match else None
        if not name and kind_node in _CSS_TYPES:
            selector = re.search(r"([^{}\n]+)\s*\{", raw)
            name = selector.group(1).strip() if selector else None
        kind = forced_kind or _symbol_type(kind_node, name or "", raw)
        if not forced_kind and kind == "function" and node_type is None:
            ancestor = getattr(node, "parent", None)
            while ancestor is not None:
                if ancestor.type in _CLASS_TYPES:
                    kind = "method"
                    break
                if ancestor.type in _FUNCTION_TYPES:
                    break
                ancestor = getattr(ancestor, "parent", None)
        if not name:
            name = PurePosixPath(path).name
        candidates.append({"start_line": start, "end_line": end, "symbol_name": name,
                           "symbol_type": kind, "content": raw, "_node_type": kind_node})

    # Named declarations and methods.
    for node in nodes:
        if node.type in _FUNCTION_TYPES or node.type in _SQL_TYPES or node.type in _CSS_TYPES or node.type in _CLASS_TYPES:
            # Anonymous arrow callbacks are included in their route/parent, not emitted as independent fragments.
            if node.type in {"arrow_function", "function_expression"}:
                continue
            add(node)
        elif node.type == "decorated_definition":
            inner = next((c for c in node.named_children if c.type in _FUNCTION_TYPES or c.type in _CLASS_TYPES), None)
            if inner is not None:
                add(node, inner.type, forced_name=_field_name(inner), forced_kind=_symbol_type(inner.type, _field_name(inner) or "", ""))

    # Arrow/function expressions assigned to a variable (including React components).
    for node in nodes:
        if node.type != "variable_declarator":
            continue
        if any(child.type in {"arrow_function", "function_expression"} for child in _walk(node) if child is not node):
            name = _field_name(node)
            raw = text.encode("utf-8")[node.start_byte:node.end_byte].decode("utf-8", "replace")
            add(node, "variable_declarator", forced_name=name, forced_kind=_symbol_type("variable_declarator", name or "", raw))

    # Express-style route registration captures path and handler together.
    for node in nodes:
        if node.type not in {"call_expression", "call"}:
            continue
        raw = text.encode("utf-8")[node.start_byte:node.end_byte].decode("utf-8", "replace")
        match = _ROUTE_RE.search(raw)
        if match:
            add(node, "call_expression", forced_name=f"{match.group(1).upper()} {match.group(3)}", forced_kind="route")

    # A class with methods gets a compact declaration/header chunk plus its individual methods.
    adjusted: list[dict] = []
    for item in candidates:
        if item["_node_type"] not in _CLASS_TYPES:
            adjusted.append(item)
            continue
        members = [c for c in candidates if c is not item and c["symbol_type"] in {"method", "function"}
                   and item["start_line"] <= c["start_line"] <= item["end_line"]]
        if not members:
            adjusted.append(item)
            continue
        first_member = min(c["start_line"] for c in members)
        header_end = max(item["start_line"], first_member - 1)
        if header_end >= item["start_line"]:
            item = dict(item)
            item["end_line"] = header_end
            item["content"] = "\n".join(lines[item["start_line"]-1:header_end])
            item["symbol_type"] = "class"
            if item["content"].strip():
                adjusted.append(item)
    candidates = adjusted

    # Prefer useful outer symbols; suppress a nested declaration already represented by its parent.
    candidates.sort(key=lambda c: (c["start_line"], -(c["end_line"] - c["start_line"]), c["symbol_name"].lower()))
    selected: list[dict] = []
    for item in candidates:
        if any(prev["symbol_type"] in {"function", "method", "route"}
               and prev["start_line"] <= item["start_line"] and prev["end_line"] >= item["end_line"]
               for prev in selected):
            continue
        if any(prev["start_line"] == item["start_line"] and prev["end_line"] == item["end_line"]
               and prev["content"] == item["content"] for prev in selected):
            continue
        selected.append(item)
    return selected

def _coverage_chunks(path: str, language: str, text: str, candidates: list[dict]) -> list[dict]:
    lines = text.splitlines()
    covered = [False] * len(lines)
    for item in candidates:
        for i in range(max(0, item["start_line"] - 1), min(len(lines), item["end_line"])):
            covered[i] = True
    result = list(candidates)
    i = 0
    while i < len(lines):
        if covered[i] or not lines[i].strip():
            i += 1
            continue
        start = i
        # Preserve nearby imports/comments/config statements together without swallowing symbol chunks.
        while i < len(lines) and not covered[i] and i - start < FALLBACK_LINES:
            i += 1
        end = i
        content = "\n".join(lines[start:end])
        if content.strip():
            result.append({"start_line": start + 1, "end_line": end, "symbol_name": PurePosixPath(path).name,
                           "symbol_type": "section", "content": content})
    deduped: list[dict] = []
    seen = set()
    for item in sorted(result, key=lambda c: (c["start_line"], c["end_line"], c["symbol_name"].lower())):
        digest = hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
        key = (item["start_line"], item["end_line"], digest)
        if key in seen:
            continue
        seen.add(key)
        item = {k: v for k, v in item.items() if not k.startswith("_")}
        item.update({"file_path": path, "language": language, "content_hash": digest})
        deduped.append(item)
    return deduped


def _notebook_chunks(path: str, text: str) -> list[dict]:
    """Extract code cells from a notebook and map code excerpts to JSON source lines."""
    try:
        notebook = json.loads(text)
    except (TypeError, ValueError):
        return []
    cells = notebook.get("cells", []) if isinstance(notebook, dict) else []
    if not isinstance(cells, list):
        return []

    # Locate each cell's serialized `source` field to retain real notebook line
    # citations while decoding escaped Python code for architecture analysis.
    source_fields = list(re.finditer(r"\"source\"\s*:", text))
    cursor = 0
    code_cells = []
    decoder = json.JSONDecoder()
    for cell_index, cell in enumerate(cells):
        if cursor >= len(source_fields):
            break
        field = source_fields[cursor]
        cursor += 1
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        value_start = field.end()
        while value_start < len(text) and text[value_start].isspace():
            value_start += 1
        strings: list[tuple[str, int]] = []
        if value_start < len(text) and text[value_start] == "[":
            position = value_start + 1
            while position < len(text):
                while position < len(text) and (text[position].isspace() or text[position] == ","):
                    position += 1
                if position >= len(text) or text[position] == "]":
                    break
                try:
                    value, end = decoder.raw_decode(text, position)
                except ValueError:
                    break
                if isinstance(value, str):
                    strings.append((value, text.count("\n", 0, position) + 1))
                position = end
        else:
            try:
                value, _end = decoder.raw_decode(text, value_start)
                if isinstance(value, str):
                    strings.append((value, text.count("\n", 0, value_start) + 1))
            except ValueError:
                continue

        code_lines: list[str] = []
        source_map: list[int] = []
        for value, source_line in strings:
            rows = value.splitlines()
            if value.endswith("\n"):
                rows.append("")
            code_lines.extend(rows)
            source_map.extend([source_line] * len(rows))
        while code_lines and not code_lines[-1].strip():
            code_lines.pop()
            source_map.pop()
        source = "\n".join(code_lines)
        if not source.strip():
            continue
        extracted = chunks(path + ".cell.py", source)
        for item in extracted:
            start, end = item["start_line"], item["end_line"]
            mapped = source_map[start - 1:end]
            if not mapped:
                continue
            item["file_path"] = path
            item["language"] = "Jupyter Notebook"
            item["symbol_name"] = f"Cell {cell_index + 1}: {item['symbol_name']}"
            item["symbol_type"] = "notebook_cell"
            item["start_line"] = min(mapped)
            item["end_line"] = max(mapped)
            item["source_line_map"] = mapped
            code = item["content"]
            item["content_hash"] = hashlib.sha256(code.encode("utf-8")).hexdigest()
            code_cells.append(item)
    return code_cells

def chunks(path: str, text: str) -> list[dict]:
    """Return bounded symbol chunks plus gap chunks that cover every nonblank source line."""
    language = language_for_path(path)
    if not language or not text.strip():
        return []
    extension = PurePosixPath(path).suffix.lower()
    if extension == ".ipynb":
        return _notebook_chunks(path, text)
    grammar = {".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
               ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".mts": "typescript",
               ".cts": "typescript", ".tsx": "tsx", ".java": "java", ".c": "c", ".h": "c",
               ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".go": "go", ".rs": "rust",
               ".html": "html", ".css": "css", ".sql": "sql", ".json": "json", ".yaml": "yaml",
               ".yml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".conf": "ini",
               ".properties": "properties", ".md": "markdown"}.get(extension)
    candidates: list[dict] = []
    if extension == ".md":
        lines = text.splitlines()
        headings = [(i, re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)) for i, line in enumerate(lines)]
        found = [(i, m) for i, m in headings if m]
        for j, (start, match) in enumerate(found):
            end = found[j+1][0] if j + 1 < len(found) else len(lines)
            if end - start <= MAX_SYMBOL_LINES:
                candidates.append({"start_line": start+1, "end_line": max(start+1,end), "symbol_name": match.group(2),
                                   "symbol_type": "section", "content": "\n".join(lines[start:end])})
    if grammar and extension != ".md":
        try:
            candidates = _syntax_candidates(path, text, grammar)
        except Exception:
            # Parser/runtime/grammar errors always fall through to the lossless line-window coverage.
            candidates = []
    return _coverage_chunks(path, language, text, candidates)
