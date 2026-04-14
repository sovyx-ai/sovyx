#!/usr/bin/env python3
"""Quality audit for 35 consolidated docs in docs/.

9 checks per doc:
1. Título + objetivo na 1a seção
2. Seção Referências com docs originais (vps-brain-dump/ ou SPE-/IMPL-/ADR-*)
3. Exemplo de código real (```python com content não-pseudo)
4. Diagrama mermaid (obrigatório em architecture/, best-effort em outros)
5. [NOT IMPLEMENTED] com explicação + ref spec (se marcador presente)
6. [DIVERGENCE] com ADR vs código (se marcador presente)
7. Sem TODO/TBD/placeholder/seção vazia
8. Sem texto genérico "this module handles..." (regex signal)
9. Tom técnico sem bullshit ("revolutionary", "cutting-edge", "blazing fast", etc.)
"""
from __future__ import annotations
import re
from pathlib import Path

DOCS_ROOT = Path("E:/sovyx/docs")
OUT = Path("E:/sovyx/docs/_meta/quality-audit-raw.md")

CATEGORIES = ["architecture", "modules", "security", "research", "planning", "development"]

BULLSHIT_WORDS = [
    r"\brevolutionary\b", r"\bcutting-edge\b", r"\bblazing[- ]fast\b",
    r"\bworld-class\b", r"\bgame-chang(er|ing)\b", r"\bnext-gen(eration)?\b",
    r"\bdisrupt(ive|ion)\b", r"\bseamless(ly)?\b", r"\bleverag(e|ing)\b",
    r"\bsynergy\b", r"\bholistic\b", r"\bparadigm shift\b",
    r"\bstate-of-the-art\b", r"\bindustry-leading\b",
    r"\bbest-in-class\b", r"\bturnkey\b", r"\bmission-critical\b",
]

GENERIC_PATTERNS = [
    r"\bthis module handles\b",
    r"^This module provides\b",
    r"^This file contains\b",
    r"\ba variety of\b",
    r"\bvarious features\b",
    r"\ba powerful\b",
    r"\ba robust\b",
    r"\bsimply (use|call|run)\b",
]

PLACEHOLDER_PATTERNS = [
    r"\bTODO\b", r"\bTBD\b", r"\bFIXME\b",
    r"\bPLACEHOLDER\b", r"\[placeholder\]", r"\[TBD\]", r"\[TODO\]",
    r"<placeholder>", r"<fill[_ -]", r"<insert[_ -]",
    r"^TODO:", r"^TBD:",
]


def check_1_title_objective(content: str) -> tuple[bool, str]:
    lines = content.splitlines()
    if not lines or not lines[0].startswith("# "):
        return False, "falta título H1 na linha 1"
    # Look for objective-like section in first 30 lines
    first_part = "\n".join(lines[:40]).lower()
    if any(k in first_part for k in ["## objetivo", "## overview", "## introdução", "## propósito", "**objetivo**", "## purpose", "## o que é"]):
        return True, "ok"
    # Is there a non-empty paragraph in first 10 lines after title?
    body_after_title = "\n".join(lines[1:15]).strip()
    if len(body_after_title) > 100:
        return True, "intro sem header mas com texto substantivo"
    return False, "sem seção Objetivo/Overview clara"


def check_2_references(content: str) -> tuple[bool, str]:
    # Look for canonical references section (allow numbered prefix like "## 11. Referências")
    has_section = bool(re.search(
        r"^##\s+(?:\d+\.?\s+)?(Referências|References|Fontes|Specs-fonte|Rastreabilidade)\b",
        content, re.MULTILINE,
    ))
    if not has_section:
        return False, "sem seção Referências/Rastreabilidade"
    # Count refs to vps-brain-dump, SPE-, IMPL-, ADR-, VR-
    ref_count = len(re.findall(r"(vps-brain-dump|SPE-\d+|IMPL-\d+|ADR-\d+|VR-\d+|sovyx-bible|SOVYX-BKD-|BKD-ADR|BKD-IMPL|BKD-SPE|BKD-VR|BKD-PRD|BKD-PLN)", content))
    if ref_count < 2:
        return False, f"só {ref_count} refs a docs originais"
    return True, f"{ref_count} refs a docs originais"


def check_3_code_example(content: str) -> tuple[bool, str]:
    code_blocks = re.findall(r"```(python|py|typescript|ts|tsx|bash|sh|yaml|yml|toml|sql|rust|go|json)\n(.*?)```", content, re.DOTALL)
    if not code_blocks:
        return False, "sem blocos de código"
    non_trivial = sum(1 for _, body in code_blocks if len(body.strip().splitlines()) >= 3)
    if non_trivial == 0:
        return False, f"{len(code_blocks)} blocos mas todos < 3 linhas"
    return True, f"{non_trivial} blocos de código não-triviais"


def check_4_mermaid(content: str, category: str) -> tuple[bool, str]:
    has_mermaid = "```mermaid" in content
    if category == "architecture":
        if has_mermaid:
            return True, "mermaid presente (obrigatório em architecture/)"
        return False, "architecture/ exige mermaid"
    # Non-architecture: best-effort; mermaid é bonus
    if has_mermaid:
        return True, "mermaid presente"
    return True, "mermaid não obrigatório fora de architecture/"


def check_5_not_implemented(content: str) -> tuple[bool, str]:
    # Find all [NOT IMPLEMENTED] markers
    markers = re.findall(r"\[NOT IMPLEMENTED\](.{0,500})", content, re.DOTALL)
    if not markers:
        return True, "sem [NOT IMPLEMENTED]"
    # For each marker, check if explanation + spec ref follows
    bad = []
    for i, m in enumerate(markers):
        # Look for spec reference within context (SPE-/IMPL-/ADR-/VR-) or "spec" / "doc" reference
        if not re.search(r"(SPE-\d+|IMPL-\d+|ADR-\d+|VR-\d+|spec|§\d|IMPL-SUP|SOVYX-BKD-)", m):
            bad.append(i + 1)
    if bad:
        return False, f"{len(markers)} [NOT IMPLEMENTED], {len(bad)} sem ref spec"
    return True, f"{len(markers)} [NOT IMPLEMENTED] todos com ref"


def check_6_divergence(content: str) -> tuple[bool, str]:
    markers = re.findall(r"\[DIVERGENCE\](.{0,500})", content, re.DOTALL)
    if not markers:
        return True, "sem [DIVERGENCE]"
    bad = []
    for i, m in enumerate(markers):
        # Explanation must mention spec/ADR AND code/implementação
        has_spec = re.search(r"(SPE-\d+|ADR-\d+|IMPL-\d+|spec|§)", m)
        has_code_ref = re.search(r"(código|implementa|code|.py|usa|implementation)", m, re.IGNORECASE)
        if not (has_spec and has_code_ref):
            bad.append(i + 1)
    if bad:
        return False, f"{len(markers)} [DIVERGENCE], {len(bad)} incompletos"
    return True, f"{len(markers)} [DIVERGENCE] todos completos"


def check_7_placeholders(content: str) -> tuple[bool, str]:
    issues: list[str] = []
    # Case-sensitive: TODO/TBD/FIXME/XXX are placeholders only in UPPERCASE
    for pat in PLACEHOLDER_PATTERNS:
        matches = re.findall(pat, content, re.MULTILINE)
        if matches:
            issues.append(f"{pat}={len(matches)}")
    # Empty sections: ## X followed by empty/whitespace until next ##
    empty_sections = re.findall(r"^##\s+([^\n]+)\n+(?=##\s)", content, re.MULTILINE)
    if empty_sections:
        issues.append(f"seções vazias={len(empty_sections)}: {empty_sections[:3]}")
    if issues:
        return False, "; ".join(issues)
    return True, "ok"


def check_8_generic(content: str) -> tuple[bool, str]:
    hits: list[str] = []
    for pat in GENERIC_PATTERNS:
        matches = re.findall(pat, content, re.IGNORECASE | re.MULTILINE)
        if matches:
            hits.append(f"{pat}={len(matches)}")
    if hits:
        return False, "; ".join(hits)
    return True, "ok"


def check_9_bullshit(content: str) -> tuple[bool, str]:
    hits: list[str] = []
    for pat in BULLSHIT_WORDS:
        matches = re.findall(pat, content, re.IGNORECASE)
        if matches:
            hits.append(f"{pat.strip(chr(92)+'b')}={len(matches)}")
    if hits:
        return False, "; ".join(hits[:5])
    return True, "ok"


def audit_doc(path: Path, category: str) -> dict:
    content = path.read_text(encoding="utf-8", errors="replace")
    checks = {
        "1_title": check_1_title_objective(content),
        "2_refs": check_2_references(content),
        "3_code": check_3_code_example(content),
        "4_mermaid": check_4_mermaid(content, category),
        "5_notimpl": check_5_not_implemented(content),
        "6_divergence": check_6_divergence(content),
        "7_placeholder": check_7_placeholders(content),
        "8_generic": check_8_generic(content),
        "9_bullshit": check_9_bullshit(content),
    }
    passed = sum(1 for _, (ok, _) in checks.items() if ok)
    return {
        "path": path.relative_to(Path("E:/sovyx")).as_posix(),
        "category": category,
        "checks": checks,
        "score": passed,
        "total": 9,
    }


def main() -> None:
    results: list[dict] = []
    for cat in CATEGORIES:
        root = DOCS_ROOT / cat
        for md in sorted(root.glob("*.md")):
            results.append(audit_doc(md, cat))
    # Summary
    total_checks = len(results) * 9
    total_passed = sum(r["score"] for r in results)
    perfect = sum(1 for r in results if r["score"] == 9)

    lines: list[str] = []
    lines.append("# Quality Audit — Raw Output")
    lines.append("")
    lines.append(f"**Docs auditados**: {len(results)}")
    lines.append(f"**Checks totais**: {total_checks}")
    lines.append(f"**Checks passados**: {total_passed} ({100*total_passed/total_checks:.1f}%)")
    lines.append(f"**Docs com 9/9**: {perfect}")
    lines.append("")
    lines.append("## Scorecard")
    lines.append("")
    lines.append("| Doc | Score | Falhas |")
    lines.append("|---|---:|---|")
    for r in sorted(results, key=lambda x: (x["score"], x["path"])):
        failures = [f"#{k.split('_')[0]}: {detail}" for k, (ok, detail) in r["checks"].items() if not ok]
        fail_str = "; ".join(failures) if failures else "—"
        lines.append(f"| `{r['path']}` | {r['score']}/9 | {fail_str} |")
    lines.append("")
    lines.append("## Detalhe por doc")
    lines.append("")
    for r in sorted(results, key=lambda x: x["path"]):
        lines.append(f"### `{r['path']}` ({r['score']}/9)")
        lines.append("")
        for k, (ok, detail) in r["checks"].items():
            mark = "✅" if ok else "❌"
            lines.append(f"- {mark} **{k}**: {detail}")
        lines.append("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"docs: {len(results)}")
    print(f"checks passed: {total_passed}/{total_checks} = {100*total_passed/total_checks:.1f}%")
    print(f"docs com 9/9: {perfect}")
    print()
    print("=== docs com falhas ===")
    for r in sorted(results, key=lambda x: x["score"]):
        if r["score"] < 9:
            failures = [k.split("_")[0] for k, (ok, _) in r["checks"].items() if not ok]
            print(f"  {r['score']}/9  {r['path']}  [falhou: {', '.join(failures)}]")


if __name__ == "__main__":
    main()
