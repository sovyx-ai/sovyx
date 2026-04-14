#!/usr/bin/env python3
"""Classify memory/nodes, memory/confidential/*, memory/archive/*, memory/daily/* via frontmatter + path heuristics."""
from __future__ import annotations
import re
from pathlib import Path

DUMP = Path("E:/sovyx/vps-brain-dump")
OUT_NODES = Path("E:/sovyx/docs/_meta/batches/triage-4-final.txt")
OUT_REST = Path("E:/sovyx/docs/_meta/batches/triage-5-final.txt")

SOVYX_KEYWORDS = [
    "sovyx", "sovereign minds", "cognitive loop", "brain graph",
    "obsidian protocol", "bunker-v2", "adhd companion",
    "plugin sandbox", "speaker recognition", "onnx runtime",
    "dashboard-react", "fastapi dashboard", "aiosqlite-deadlock",
    "cognitive_loop", "sovyx-cli", "sovyx cli",
]
EREBUS_KEYWORDS = [
    "erebus", "arbitragem", "arbitrage", "kalshi", "polymarket",
    "avellaneda", "weather-trading", "prediction market",
    "settlement engine", "edge-hunter", "edge hunter", "cme ",
    "forecastex", "backtest", "venue", "order book", "market making",
    "divergence matrix", "dark-intel", "dark intel", "risk engine",
    "fund structure", "hedge fund", "anbima", "cvm-",
    "weather", "metar", "asos",
]


def read_frontmatter(path: Path, n: int = 100):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}, ""
    lines = text.split("\n")
    fm = {}
    if lines and lines[0].strip() == "---":
        for i in range(1, min(60, len(lines))):
            if lines[i].strip() == "---":
                break
            m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_-]*):\s*(.*)$", lines[i])
            if m:
                fm[m.group(1).lower()] = m.group(2).strip()
    snippet = "\n".join(lines[:n]).lower()
    return fm, snippet


def classify(path: Path):
    name = path.name
    fm, snippet = read_frontmatter(path)
    domain = fm.get("domain", "").lower()
    title = fm.get("title", "").strip('"').strip()
    doc_id = fm.get("id", name.replace(".md", ""))

    erebus_domains_exact = {
        "erebus", "trading", "regulatory", "prediction-markets",
        "risk-management", "market-universe", "market-intelligence",
        "erebus-platform", "calibration", "weather-trading",
        "hedge-fund-infrastructure", "fund-structure",
        "tax-strategy", "tax", "infrastructure",
    }
    sovyx_domains_map = {
        "sovyx-dashboard": "SOVYX-FRONTEND",
        "sovyx-architecture": "SOVYX-CORE",
        "sovyx-strategy": "SOVYX-PLANNING",
        "sovyx": "SOVYX-CORE",
        "frontend": "SOVYX-FRONTEND",
        "nyx-core": "SOVYX-CORE",
        "engineering": "SOVYX-BACKEND",
        "research": None,
    }

    if domain:
        first_tok = domain.split(",")[0].strip()
        if first_tok in sovyx_domains_map:
            cat = sovyx_domains_map[first_tok]
            if cat:
                summary = (title or doc_id or name)[:115]
                return cat, summary, "alta"
        if first_tok in erebus_domains_exact or first_tok.startswith("erebus") or \
           first_tok.startswith("trading") or first_tok.startswith("kalshi") or \
           first_tok.startswith("polymarket") or first_tok.startswith("tax"):
            summary = f"Erebus: {title or doc_id or name}"[:115]
            return "IRRELEVANT", summary, "baixa"

    low_name = name.lower()

    if re.match(r"^CODEX-", name):
        return "IRRELEVANT", f"Erebus CODEX: {title or doc_id}"[:115], "baixa"

    erebus_prefixes = (
        "edge-hunter", "dark-intel", "divergence-matrix", "arb-engine",
        "erebus-", "forecastex", "nyx-edge", "ibkr", "kalshi", "polymarket",
        "cme-", "cf-benchmarks", "city-", "weather-", "academic-papers",
        "avellaneda", "compliance-officer", "audit-trail", "capital-allocation",
        "correlation-", "market-", "portfolio-", "tax-", "regulatory-",
        "settlement-", "venue-", "btc-", "prediction-", "climate-",
        "commodities-", "competitive-landscape-prediction", "cvm-",
        "anbima-", "hedge-fund", "fund-structure", "liquidity-",
        "solana-", "order-", "black-swan", "backtest-", "calibration-",
        "nats-", "overlay", "questdb", "ibgw-", "additional-venues",
        "exchanges-", "cli-metar", "strategies-edges", "pricing-",
        "smt-", "risk-", "cashing-", "architecture-exchange",
        "architecture-nats", "nyx-arch", "nyx-cortex", "nyx-thoughts",
        "nyx-aurora", "nyx-operational", "nyx-memory",
        "competitive-landscape-prediction", "competitors-analysis",
        "constraint-root", "guipe-positioning",
    )
    if low_name.startswith(erebus_prefixes):
        return "IRRELEVANT", f"Erebus: {title or doc_id or name}"[:115], "baixa"

    if "sovyx" in low_name or "obsidian" in low_name or "bunker-v2" in low_name:
        if "voice" in low_name or "bunker-v2" in low_name:
            cat = "SOVYX-VOICE"
        elif "dashboard" in low_name or "frontend" in low_name or "dash-" in low_name or low_name.startswith("sovyx-imm-f"):
            cat = "SOVYX-FRONTEND"
        elif "brain" in low_name or "dynamic-importance" in low_name:
            cat = "SOVYX-BRAIN"
        elif "plugin" in low_name:
            cat = "SOVYX-PLUGINS"
        elif "ci-" in low_name or "deadlock" in low_name or "aiosqlite" in low_name:
            cat = "SOVYX-BACKEND"
        elif "cognitive" in low_name or "cogloop" in low_name:
            cat = "SOVYX-COGNITIVE"
        elif "llm" in low_name or "router" in low_name or "ollama" in low_name:
            cat = "SOVYX-LLM"
        elif "cloud" in low_name or "pricing" in low_name or "enterprise" in low_name or "revenue" in low_name:
            cat = "SOVYX-CLOUD"
        elif "observability" in low_name or "obs-" in low_name or "metrics" in low_name:
            cat = "SOVYX-OBSERVABILITY"
        elif "security" in low_name or "privacy" in low_name or "compliance" in low_name or "identity" in low_name or "trademark" in low_name:
            cat = "SOVYX-SECURITY"
        elif "persistence" in low_name or "d4-persistence" in low_name or "d3-concurrency" in low_name:
            cat = "SOVYX-PERSISTENCE"
        elif "upgrade" in low_name or "packaging" in low_name or "d6-bootstrap" in low_name:
            cat = "SOVYX-UPGRADE"
        elif "bridge" in low_name or "communication" in low_name or "relay" in low_name or "telegram" in low_name or "signal" in low_name:
            cat = "SOVYX-BRIDGE"
        elif "emotional" in low_name or "proactive" in low_name or "mind" in low_name or "personality" in low_name:
            cat = "SOVYX-MIND"
        elif "research" in low_name or "case-stud" in low_name or "viral" in low_name or "channels" in low_name or "gtm" in low_name or "competitive" in low_name or "license" in low_name or "hype" in low_name or "depth" in low_name:
            cat = "SOVYX-RESEARCH"
        elif "planning" in low_name or "roadmap" in low_name or "strategy" in low_name or "mission" in low_name:
            cat = "SOVYX-PLANNING"
        elif "testing" in low_name or "test" in low_name or "perf" in low_name:
            cat = "SOVYX-BACKEND"
        else:
            cat = "SOVYX-CORE"
        return cat, (title or doc_id or name)[:115], "alta"

    sovyx_hits = sum(1 for k in SOVYX_KEYWORDS if k in snippet)
    erebus_hits = sum(1 for k in EREBUS_KEYWORDS if k in snippet)

    # Nyx Prediction Markets prefix = Erebus legacy name
    if "nyx" in snippet and ("prediction market" in snippet or "kalshi" in snippet or
                              "polymarket" in snippet or "weather" in snippet or
                              "arbitrag" in snippet):
        return "IRRELEVANT", f"Erebus/Nyx-prediction: {title or doc_id or name}"[:115], "baixa"

    if erebus_hits >= 2 and erebus_hits > sovyx_hits:
        return "IRRELEVANT", f"Erebus-content: {title or doc_id or name}"[:115], "baixa"
    if erebus_hits >= 1 and sovyx_hits == 0:
        return "IRRELEVANT", f"Erebus-hint: {title or doc_id or name}"[:115], "baixa"
    if sovyx_hits >= 2 and sovyx_hits > erebus_hits:
        if "voice" in snippet and "tts" in snippet:
            cat = "SOVYX-VOICE"
        elif "react" in snippet and "dashboard" in snippet:
            cat = "SOVYX-FRONTEND"
        elif "plugin" in snippet and "sandbox" in snippet:
            cat = "SOVYX-PLUGINS"
        elif "brain" in snippet and "graph" in snippet:
            cat = "SOVYX-BRAIN"
        elif "cognitive" in snippet or "react loop" in snippet or "ooda" in snippet:
            cat = "SOVYX-COGNITIVE"
        elif "llm router" in snippet or "provider" in snippet and "anthropic" in snippet:
            cat = "SOVYX-LLM"
        else:
            cat = "SOVYX-CORE"
        return cat, (title or doc_id or name)[:115], "media"

    if erebus_hits > 0 and sovyx_hits > 0:
        return "MIXED", f"Misto Sovyx/Erebus: {title or doc_id or name}"[:115], "baixa"
    if erebus_hits > 0:
        return "IRRELEVANT", f"Erebus: {title or doc_id or name}"[:115], "baixa"
    if sovyx_hits >= 1:
        # Only 1 sovyx hit — require explicit sovyx mention in title/id to be safe
        if "sovyx" in (title + doc_id + name).lower():
            return "SOVYX-CORE", (title or doc_id or name)[:115], "media"
        return "MIXED", f"Pouco-signal Sovyx: {title or doc_id or name}"[:115], "baixa"

    # Zero signals: check daily dates (diary = planning)
    if re.match(r"^\d{4}-\d{2}-\d{2}(-.*)?\.md$", name):
        return "SOVYX-PLANNING", f"Diario: {name}"[:115], "baixa"
    if "archive" in str(path).lower():
        return "IRRELEVANT", f"Archive (nao classificado): {name}"[:115], "baixa"
    return "MIXED", f"Ambiguo: {title or doc_id or name}"[:115], "baixa"


def normalize(s: str) -> str:
    s = re.sub(r"[\r\n]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    s = s.replace("|", "/")
    return s.strip()


def process_list(list_file: Path, out_file: Path):
    paths = []
    for line in list_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        p = Path(line)
        if not p.is_absolute():
            p = Path("E:/sovyx") / line
        paths.append(p)
    out_lines = []
    counts: dict[str, int] = {}
    for p in paths:
        if not p.exists():
            rel = p.name
            out_lines.append(f"{rel}|ERROR|file not found|baixa")
            counts["ERROR"] = counts.get("ERROR", 0) + 1
            continue
        cat, summary, rel_score = classify(p)
        summary = normalize(summary)
        rel = str(p.relative_to(Path("E:/sovyx"))).replace("\\", "/")
        out_lines.append(f"{rel}|{cat}|{summary}|{rel_score}")
        counts[cat] = counts.get(cat, 0) + 1
    out_file.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return counts


print("=== BATCH 4 (nodes, 355) ===")
c4 = process_list(Path("E:/sovyx/docs/_meta/batches/batch4-nodes.txt"), OUT_NODES)
for k, v in sorted(c4.items(), key=lambda x: -x[1]):
    print(f"  {v:4d} {k}")
print(f"Total: {sum(c4.values())}")
print()
print("=== BATCH 5 (rest, 183) ===")
c5 = process_list(Path("E:/sovyx/docs/_meta/batches/batch5-rest.txt"), OUT_REST)
for k, v in sorted(c5.items(), key=lambda x: -x[1]):
    print(f"  {v:4d} {k}")
print(f"Total: {sum(c5.values())}")
