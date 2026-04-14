#!/usr/bin/env python3
"""Consolidate 5 triage files into docs/_triage-index.md."""
from __future__ import annotations
from pathlib import Path
from collections import defaultdict

META = Path("E:/sovyx/docs/_meta/batches")
FILES = [
    META / "triage-1.txt",
    META / "triage-2.txt",
    META / "triage-3.txt",
    META / "triage-4-final.txt",
    META / "triage-5-final.txt",
]
OUT = Path("E:/sovyx/docs/_meta/triage-index.md")

entries: list[tuple[str, str, str, str]] = []
for f in FILES:
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        entries.append(tuple(parts))

# Counts
cat_counts: dict[str, int] = defaultdict(int)
rel_counts: dict[str, int] = defaultdict(int)
cat_entries: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)

for e in entries:
    path, cat, summary, rel = e
    cat_counts[cat] += 1
    rel_counts[rel] += 1
    cat_entries[cat].append(e)

# Category ordering
SOVYX_ORDER = [
    "SOVYX-CORE", "SOVYX-PLANNING", "SOVYX-RESEARCH",
    "SOVYX-BACKEND", "SOVYX-COGNITIVE", "SOVYX-BRAIN",
    "SOVYX-CONTEXT", "SOVYX-PERSISTENCE", "SOVYX-LLM",
    "SOVYX-VOICE", "SOVYX-FRONTEND", "SOVYX-PLUGINS",
    "SOVYX-BRIDGE", "SOVYX-CLOUD", "SOVYX-OBSERVABILITY",
    "SOVYX-UPGRADE", "SOVYX-SECURITY", "SOVYX-MIND",
    "SOVYX-BRAND", "SOVYX-MISSION",
    "MIXED", "IRRELEVANT",
]
# Add any unknown category at the end
for c in sorted(cat_counts.keys()):
    if c not in SOVYX_ORDER:
        SOVYX_ORDER.append(c)

total = len(entries)
sovyx_total = sum(v for k, v in cat_counts.items() if k.startswith("SOVYX-"))
mixed_total = cat_counts.get("MIXED", 0)
irrel_total = cat_counts.get("IRRELEVANT", 0)

lines: list[str] = []
lines.append("# Triage Index — 853 arquivos do VPS brain dump")
lines.append("")
lines.append("**Fonte**: `vps-brain-dump/` (cópia temporária de `/root/.openclaw/workspace/` no VPS 216.238.111.224)")
lines.append(f"**Total**: {total} arquivos `.md` classificados")
lines.append(f"**Gerado em**: 2026-04-14 (dentro da missão de reescrita completa da doc Sovyx)")
lines.append("")
lines.append("## Sumário por relevância pro projeto Sovyx")
lines.append("")
lines.append("| Bucket | Arquivos | % |")
lines.append("|---|---|---|")
lines.append(f"| **Sovyx** (SOVYX-*) | {sovyx_total} | {100*sovyx_total/total:.1f}% |")
lines.append(f"| **MIXED** (conteúdo Sovyx + outro projeto) | {mixed_total} | {100*mixed_total/total:.1f}% |")
lines.append(f"| **IRRELEVANT** (Erebus, Openclaw, pentests, política) | {irrel_total} | {100*irrel_total/total:.1f}% |")
lines.append("")
lines.append("## Sumário por categoria (ordenado por volume)")
lines.append("")
lines.append("| Categoria | Arquivos |")
lines.append("|---|---|")
for cat in sorted(cat_counts.keys(), key=lambda c: -cat_counts[c]):
    lines.append(f"| {cat} | {cat_counts[cat]} |")
lines.append("")
lines.append("## Sumário por relevância")
lines.append("")
lines.append("| Nível | Arquivos |")
lines.append("|---|---|")
for rel in ["alta", "media", "baixa"]:
    if rel in rel_counts:
        lines.append(f"| {rel} | {rel_counts[rel]} |")
lines.append("")
lines.append("---")
lines.append("")
lines.append("## Índice por categoria")
lines.append("")
lines.append("Formato por linha: `path | resumo | relevância`.")
lines.append("")

for cat in SOVYX_ORDER:
    if cat not in cat_entries:
        continue
    lines.append(f"### {cat} ({cat_counts[cat]})")
    lines.append("")
    # Sort: alta > media > baixa, then by path
    rel_rank = {"alta": 0, "media": 1, "baixa": 2}
    sorted_entries = sorted(
        cat_entries[cat],
        key=lambda e: (rel_rank.get(e[3], 3), e[0]),
    )
    for path, _cat, summary, rel in sorted_entries:
        # Compact form
        lines.append(f"- `{path}` — {summary} _(rel: {rel})_")
    lines.append("")

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"wrote {OUT}")
print(f"total entries: {total}")
print(f"sovyx: {sovyx_total}, mixed: {mixed_total}, irrelevant: {irrel_total}")
print()
print("by category:")
for cat in sorted(cat_counts.keys(), key=lambda c: -cat_counts[c]):
    print(f"  {cat_counts[cat]:4d} {cat}")
