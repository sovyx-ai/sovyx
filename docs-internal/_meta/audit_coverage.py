#!/usr/bin/env python3
"""Audit coverage: every public class/function in src/sovyx/ must appear in docs/."""
from __future__ import annotations
import ast
import re
from collections import defaultdict
from pathlib import Path

SRC = Path("E:/sovyx/src/sovyx")
DOCS_ROOTS = [
    Path("E:/sovyx/docs/architecture"),
    Path("E:/sovyx/docs/modules"),
    Path("E:/sovyx/docs/security"),
    Path("E:/sovyx/docs/research"),
    Path("E:/sovyx/docs/planning"),
    Path("E:/sovyx/docs/development"),
]
META_DOCS = [
    Path("E:/sovyx/docs/_meta/triage-index.md"),
    Path("E:/sovyx/docs/_meta/gap-analysis.md"),
    Path("E:/sovyx/docs/_meta/source-mapping.md"),
]
OUT = Path("E:/sovyx/docs/_meta/coverage-audit.md")

SKIP_FILES = {"__init__.py", "__main__.py"}


def extract_public_symbols(py_file: Path) -> list[tuple[str, str, int]]:
    """Extract (kind, name, lineno) for public symbols. Kind in {class, function, method}."""
    try:
        src = py_file.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(py_file))
    except (SyntaxError, UnicodeDecodeError):
        return []
    out: list[tuple[str, str, int]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            out.append(("class", node.name, node.lineno))
            # Public methods
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith("_"):
                        continue
                    out.append(("method", f"{node.name}.{item.name}", item.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            out.append(("function", node.name, node.lineno))
    return out


def load_all_docs() -> dict[str, str]:
    """Map doc_relative_path -> content."""
    contents: dict[str, str] = {}
    for root in DOCS_ROOTS:
        for md in root.rglob("*.md"):
            rel = md.relative_to(Path("E:/sovyx")).as_posix()
            contents[rel] = md.read_text(encoding="utf-8", errors="replace")
    for md in META_DOCS:
        if md.exists():
            rel = md.relative_to(Path("E:/sovyx")).as_posix()
            contents[rel] = md.read_text(encoding="utf-8", errors="replace")
    return contents


def find_symbol_in_docs(symbol: str, docs: dict[str, str]) -> list[str]:
    """Return list of doc paths where the symbol appears (word-boundary match)."""
    # For methods, use the method name for matching (Class.method might be too restrictive)
    if "." in symbol:
        # "Class.method" — look for either exact "Class.method" or just "method" as a function ref
        exact = symbol
        simple = symbol.split(".")[-1]
        patterns = [
            re.compile(rf"\b{re.escape(exact)}\b"),
            re.compile(rf"\b{re.escape(simple)}\("),
        ]
    else:
        patterns = [re.compile(rf"\b{re.escape(symbol)}\b")]
    found: list[str] = []
    for path, content in docs.items():
        for pat in patterns:
            if pat.search(content):
                found.append(path)
                break
    return found


def main() -> None:
    docs = load_all_docs()
    print(f"loaded {len(docs)} docs")

    # Collect all symbols
    by_file: dict[str, list[tuple[str, str, int]]] = {}
    for py in sorted(SRC.rglob("*.py")):
        if py.name in SKIP_FILES:
            continue
        if "__pycache__" in py.parts:
            continue
        rel = py.relative_to(Path("E:/sovyx")).as_posix()
        symbols = extract_public_symbols(py)
        if symbols:
            by_file[rel] = symbols

    total = sum(len(s) for s in by_file.values())
    print(f"extracted {total} public symbols from {len(by_file)} files")

    # Categorize: class-level matching is strict; methods are best-effort
    results: dict[str, list[dict]] = defaultdict(list)
    docs_count = 0
    undoc_count = 0
    class_count = 0
    class_doc = 0
    function_count = 0
    function_doc = 0
    method_count = 0
    method_doc = 0

    for file_path, symbols in by_file.items():
        for kind, name, lineno in symbols:
            found = find_symbol_in_docs(name, docs)
            status = "✅" if found else "❌"
            if found:
                docs_count += 1
            else:
                undoc_count += 1
            if kind == "class":
                class_count += 1
                if found:
                    class_doc += 1
            elif kind == "function":
                function_count += 1
                if found:
                    function_doc += 1
            else:
                method_count += 1
                if found:
                    method_doc += 1
            results[file_path].append({
                "kind": kind,
                "name": name,
                "line": lineno,
                "found_in": found,
                "status": status,
            })

    # Write report
    lines: list[str] = []
    lines.append("# Coverage Audit — Public Symbols in Docs")
    lines.append("")
    lines.append("**Gerado em**: 2026-04-14")
    lines.append(f"**Escopo**: `src/sovyx/` (todas funções/classes/métodos públicos, sem `_` prefix)")
    lines.append(f"**Docs cruzados**: {len(docs)} arquivos em `docs/` (exclui `_meta/batches`, `_meta/gap-inputs`)")
    lines.append("")
    lines.append("## Sumário")
    lines.append("")
    lines.append("| Categoria | Total | Documentado | % |")
    lines.append("|---|---:|---:|---:|")
    lines.append(f"| Classes | {class_count} | {class_doc} | {100*class_doc/class_count:.1f}% |")
    lines.append(f"| Funções top-level | {function_count} | {function_doc} | {100*function_doc/function_count:.1f}% |" if function_count else "| Funções top-level | 0 | 0 | — |")
    lines.append(f"| Métodos públicos | {method_count} | {method_doc} | {100*method_doc/method_count:.1f}% |")
    lines.append(f"| **TOTAL** | **{total}** | **{docs_count}** | **{100*docs_count/total:.1f}%** |")
    lines.append("")

    # Aggregate undocumented by module
    undoc_by_module: dict[str, int] = defaultdict(int)
    for file_path, syms in results.items():
        # module = parent folder of file (e.g., src/sovyx/engine/foo.py → engine)
        parts = Path(file_path).parts
        # ('src', 'sovyx', '<module>', ...)
        module = parts[2] if len(parts) >= 3 else "root"
        for s in syms:
            if s["status"] == "❌":
                undoc_by_module[module] += 1

    lines.append("## Undocumented por módulo")
    lines.append("")
    lines.append("| Módulo | Símbolos não-documentados |")
    lines.append("|---|---:|")
    for mod, n in sorted(undoc_by_module.items(), key=lambda x: -x[1]):
        lines.append(f"| {mod} | {n} |")
    lines.append("")

    lines.append("## Detalhe por arquivo")
    lines.append("")

    for file_path in sorted(results.keys()):
        syms = results[file_path]
        doc_cnt = sum(1 for s in syms if s["status"] == "✅")
        undoc_cnt = sum(1 for s in syms if s["status"] == "❌")
        lines.append(f"### `{file_path}` — {doc_cnt} ✅ / {undoc_cnt} ❌")
        lines.append("")
        lines.append("| Kind | Symbol | Line | Status | Documented in |")
        lines.append("|---|---|---:|---|---|")
        for s in syms:
            docs_str = ", ".join(s["found_in"][:3]) if s["found_in"] else "—"
            if len(s["found_in"]) > 3:
                docs_str += f" +{len(s['found_in'])-3}"
            lines.append(f"| {s['kind']} | `{s['name']}` | {s['line']} | {s['status']} | {docs_str} |")
        lines.append("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"coverage: {docs_count}/{total} = {100*docs_count/total:.1f}%")
    print(f"classes: {class_doc}/{class_count} = {100*class_doc/class_count:.1f}%")
    print(f"methods: {method_doc}/{method_count} = {100*method_doc/method_count:.1f}%")
    if function_count:
        print(f"functions: {function_doc}/{function_count} = {100*function_doc/function_count:.1f}%")
    print()
    print("undocumented by module:")
    for mod, n in sorted(undoc_by_module.items(), key=lambda x: -x[1]):
        print(f"  {mod}: {n}")


if __name__ == "__main__":
    main()
