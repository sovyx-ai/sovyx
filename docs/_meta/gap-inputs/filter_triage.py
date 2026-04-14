"""Filter triage-index into per-module relevant docs."""
from pathlib import Path
import re

TRIAGE = Path("E:/sovyx/docs/_meta/triage-index.md")
OUT = Path("E:/sovyx/docs/_meta/gap-inputs")

# Read triage and collect Sovyx/MIXED entries (path | summary | rel)
entries = []
cur_cat = None
for line in TRIAGE.read_text(encoding="utf-8").splitlines():
    m = re.match(r"^### ([A-Z-]+) \(", line)
    if m:
        cur_cat = m.group(1)
        continue
    m = re.match(r"^- `([^`]+)` — (.+) _\(rel: (\w+)\)_$", line)
    if m and cur_cat:
        entries.append((cur_cat, m.group(1), m.group(2), m.group(3)))

# Category to module mapping
CAT_TO_MOD = {
    "SOVYX-CORE": ["engine", "cli"],
    "SOVYX-BACKEND": ["engine", "cli", "persistence"],
    "SOVYX-COGNITIVE": ["cognitive"],
    "SOVYX-BRAIN": ["brain"],
    "SOVYX-CONTEXT": ["context"],
    "SOVYX-MIND": ["mind"],
    "SOVYX-LLM": ["llm"],
    "SOVYX-VOICE": ["voice"],
    "SOVYX-PERSISTENCE": ["persistence"],
    "SOVYX-OBSERVABILITY": ["observability"],
    "SOVYX-PLUGINS": ["plugins"],
    "SOVYX-BRIDGE": ["bridge"],
    "SOVYX-CLOUD": ["cloud"],
    "SOVYX-UPGRADE": ["upgrade"],
    "SOVYX-FRONTEND": ["dashboard"],
    "SOVYX-SECURITY": ["engine", "cloud", "plugins"],
    "SOVYX-RESEARCH": ["_research"],
    "SOVYX-PLANNING": ["_planning"],
    "SOVYX-BRAND": ["_brand"],
    "SOVYX-MISSION": ["_mission"],
    "MIXED": ["_mixed"],
}

by_module: dict[str, list] = {}
for cat, path, summary, rel in entries:
    if cat not in CAT_TO_MOD:
        continue
    for mod in CAT_TO_MOD[cat]:
        by_module.setdefault(mod, []).append((cat, path, summary, rel))

# Write per-module triage
for mod, ents in by_module.items():
    out = OUT / f"triage-{mod}.txt"
    lines = [f"=== Triage entries for module: {mod} ({len(ents)} docs) ==="]
    ents.sort(key=lambda e: ({"alta":0,"media":1,"baixa":2}.get(e[3],3), e[0], e[1]))
    for cat, path, summary, rel in ents:
        lines.append(f"[{cat}|{rel}] {path}")
        lines.append(f"  {summary}")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"{mod}: {len(ents)} docs -> {out}")

print(f"\nTotal entries processed: {len(entries)}")
