#!/usr/bin/env python3
"""prompt_runner — interactive operator prompts (T3 of AUDIT v3+).

Reads a catalog of prompts, guides the operator through each, writes
responses atomically to operator_responses.json. Supports file-path
attachments (copied into operator_attachments/).

Usage:
    prompt_runner.py --outdir DIAG_DIR --catalog CATALOG_JSON \
                     --attachments-dir ATTACH_DIR --output RESPONSES_JSON

Skip convention:
    Type ``SKIP reason`` as the first line to skip with documented reason.
    Type ``DONE`` or Ctrl-D on empty input to finish the prompt.

Design choices:
    - Multi-line input via input() loop terminated by blank line OR DONE.
    - Artifacts: glob-resolve the declared paths under outdir and
      show existing files + sizes + suggested playback commands (auto-
      detects mpv/paplay/aplay).
    - Non-leading questions (avoid "did it sound X?" — ask "describe").
    - Responses persisted atomically after EVERY prompt answered, so a
      mid-run Ctrl-C preserves what's been said so far.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


# ─── ANSI helpers (gated on TTY) ───────────────────────────────────

def _color(code: str, s: str) -> str:
    return s if not sys.stderr.isatty() else f"\033[{code}m{s}\033[0m"


BOLD = lambda s: _color("1", s)
CYAN = lambda s: _color("1;36", s)
GREEN = lambda s: _color("1;32", s)
YELLOW = lambda s: _color("1;33", s)
RED = lambda s: _color("1;31", s)
DIM = lambda s: _color("2", s)


# ─── Utilities ─────────────────────────────────────────────────────

def _now_pair() -> dict:
    """Atomic UTC + monotonic pair."""
    ns = time.time_ns()
    mono = time.monotonic_ns()
    secs = ns // 1_000_000_000
    frac = ns % 1_000_000_000
    dt = _dt.datetime.fromtimestamp(secs, tz=_dt.timezone.utc)
    iso = f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{frac:09d}Z"
    return {"responded_at_utc_ns": iso, "responded_at_monotonic_ns": mono}


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        encoding="utf-8",
    ) as tmp:
        tmp.write(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, str(path))


def _detect_audio_player() -> str | None:
    """Return a suggested playback command template or None."""
    for cand in ("mpv", "paplay", "aplay", "ffplay", "play"):
        if shutil.which(cand):
            return cand
    return None


def _resolve_artifacts(outdir: Path, patterns: list[str]) -> list[tuple[Path, int]]:
    """Glob-resolve patterns under outdir. Return (path, size_bytes)."""
    hits: list[tuple[Path, int]] = []
    for pat in patterns:
        for p in sorted(outdir.glob(pat)):
            if p.is_file():
                try:
                    hits.append((p, p.stat().st_size))
                except OSError:
                    continue
    return hits


def _read_multiline_or_skip() -> tuple[str, str | None]:
    """Read multi-line input. Return (response, skip_reason).
    Terminates on: blank line, 'DONE', or EOF.
    If input starts with 'SKIP ', returns ('', <reason>).
    """
    lines: list[str] = []
    print(DIM("   (digite sua resposta; linha em branco OU 'DONE' para finalizar; "
              "'SKIP <razão>' para pular)"), file=sys.stderr)
    try:
        while True:
            try:
                line = input()
            except EOFError:
                break
            stripped = line.strip()
            if not lines and stripped.upper().startswith("SKIP"):
                # First line is a skip instruction.
                reason = stripped[4:].strip() or "unspecified"
                return "", reason
            if stripped.upper() == "DONE":
                break
            if not stripped and lines:
                # Blank line after content = end.
                break
            if not stripped:
                # Blank before content; ignore and continue.
                continue
            lines.append(line.rstrip("\n"))
    except KeyboardInterrupt:
        print("\n  (interrupted — saving partial)", file=sys.stderr)
        raise
    return "\n".join(lines).strip(), None


def _print_prompt_header(n: int, total: int, prompt: dict) -> None:
    prio = prompt["priority"]
    prio_colored = {"P1": RED("P1"), "P2": YELLOW("P2"), "P3": CYAN("P3")}.get(
        prio, prio
    )
    print(file=sys.stderr)
    print(BOLD(f"  ─── Prompt {n}/{total} [{prio_colored}] {prompt['prompt_id']} ───"),
          file=sys.stderr)
    print(DIM(f"  Hipótese: {prompt['hypothesis']} | Categoria: {prompt['category']}"),
          file=sys.stderr)
    print(file=sys.stderr)
    print(f"  {prompt['prompt_text']}", file=sys.stderr)
    print(file=sys.stderr)


def _print_artifact_hints(artifacts: list[tuple[Path, int]], player: str | None) -> None:
    if not artifacts:
        return
    print(DIM("  Artifacts relevantes:"), file=sys.stderr)
    for path, size in artifacts:
        size_kb = size / 1024
        print(DIM(f"    • {path}  ({size_kb:.1f} KB)"), file=sys.stderr)
    if player and any(str(p).endswith(".wav") for p, _ in artifacts):
        first_wav = next((p for p, _ in artifacts if str(p).endswith(".wav")), None)
        if first_wav:
            print(DIM(f"  Sugestão de playback:  {player} {first_wav}"),
                  file=sys.stderr)
    print(file=sys.stderr)


def _copy_attachment_if_path_response(
    response: str, attach_dir: Path, prompt_id: str,
) -> list[dict]:
    """If the response text contains a filesystem path that looks like
    a file the operator referenced, copy it into attach_dir and return
    metadata. Non-destructive (copy, not move)."""
    attachments: list[dict] = []
    for token in response.split():
        cand = token.strip("'\"").rstrip(".,;")
        if not cand.startswith(("/", "~")) and cand[:3] not in ("./", "../"):
            continue
        expanded = Path(cand).expanduser()
        if not expanded.is_file():
            continue
        dest_name = f"{prompt_id}__{expanded.name}"
        dest = attach_dir / dest_name
        try:
            shutil.copy2(expanded, dest)
            size = dest.stat().st_size
            attachments.append(
                {
                    "prompt_id": prompt_id,
                    "source_path": str(expanded),
                    "attachment_rel": f"_diagnostics/operator_attachments/{dest_name}",
                    "size_bytes": size,
                }
            )
        except OSError as exc:
            attachments.append(
                {
                    "prompt_id": prompt_id,
                    "source_path": str(expanded),
                    "attachment_rel": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return attachments


# ─── Main flow ─────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True, help="diag outdir root")
    ap.add_argument("--catalog", required=True, help="prompts_catalog.json path")
    ap.add_argument("--attachments-dir", required=True)
    ap.add_argument("--output", required=True, help="operator_responses.json path")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    attach_dir = Path(args.attachments_dir)
    attach_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output)

    try:
        catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(RED(f"  [ERROR] failed to load prompt catalog: {exc}"), file=sys.stderr)
        return 1

    prompts = catalog.get("prompts", [])
    if not prompts:
        print(YELLOW("  [WARN] catalog has no prompts"), file=sys.stderr)
        return 1

    player = _detect_audio_player()
    if player:
        print(DIM(f"  Audio player detectado para sugestão: {player}"),
              file=sys.stderr)
    else:
        print(DIM("  Nenhum audio player detectado; você precisará abrir WAVs manualmente."),
              file=sys.stderr)
    print(file=sys.stderr)

    responses: list[dict] = []
    attachments: list[dict] = []
    total = len(prompts)

    # Start with an empty persisted state so a Ctrl-C before first
    # answer still shows intent.
    _atomic_write_json(
        output_path,
        {
            "schema_version": catalog.get("schema_version", 1),
            "catalog_prompt_count": total,
            "status": "in_progress",
            "responses": responses,
            "attachments": attachments,
        },
    )

    interrupted = False
    for idx, prompt in enumerate(prompts, start=1):
        _print_prompt_header(idx, total, prompt)
        arts = _resolve_artifacts(outdir, prompt.get("artifact_paths", []))
        _print_artifact_hints(arts, player)

        try:
            response_text, skip_reason = _read_multiline_or_skip()
        except KeyboardInterrupt:
            interrupted = True
            break

        record = {
            "prompt_id": prompt["prompt_id"],
            "priority": prompt["priority"],
            "category": prompt["category"],
            "hypothesis": prompt["hypothesis"],
            "prompt_text": prompt["prompt_text"],
            "response": response_text,
            "skipped": skip_reason is not None,
            "skip_reason": skip_reason,
            "artifacts_referenced": [str(p) for p, _ in arts],
            **_now_pair(),
        }

        # If response text contains file paths that exist, copy them
        # as attachments (non-destructive).
        if response_text and not skip_reason:
            new_attach = _copy_attachment_if_path_response(
                response_text, attach_dir, prompt["prompt_id"]
            )
            if new_attach:
                attachments.extend(new_attach)
                record["attachments_copied"] = [
                    a["attachment_rel"] for a in new_attach if a.get("attachment_rel")
                ]

        responses.append(record)

        # Feedback inline + persist atomically after EACH response so
        # progress is never lost.
        if skip_reason:
            print(YELLOW(f"  → SKIPPED ({skip_reason})"), file=sys.stderr)
        elif response_text:
            word_count = len(response_text.split())
            print(GREEN(f"  → registrado ({word_count} palavras)"), file=sys.stderr)
            if record.get("attachments_copied"):
                print(GREEN(f"    → {len(record['attachments_copied'])} attachment(s) copied"),
                      file=sys.stderr)
        else:
            print(DIM("  → resposta vazia"), file=sys.stderr)

        _atomic_write_json(
            output_path,
            {
                "schema_version": catalog.get("schema_version", 1),
                "catalog_prompt_count": total,
                "status": "in_progress",
                "responses": responses,
                "attachments": attachments,
            },
        )

    # Final persisted state.
    final_status = "interrupted" if interrupted else "complete"
    _atomic_write_json(
        output_path,
        {
            "schema_version": catalog.get("schema_version", 1),
            "catalog_prompt_count": total,
            "responded_count": len(responses),
            "skipped_count": sum(1 for r in responses if r["skipped"]),
            "status": final_status,
            "responses": responses,
            "attachments": attachments,
            **_now_pair(),
        },
    )

    print(file=sys.stderr)
    print(GREEN(f"  ✓ Prompts finalizados: {len(responses)}/{total} respondidos "
                f"({sum(1 for r in responses if r['skipped'])} skipped)"),
          file=sys.stderr)
    print(DIM(f"  Registro: {output_path}"), file=sys.stderr)
    print(file=sys.stderr)

    return 0 if not interrupted else 130


if __name__ == "__main__":
    sys.exit(main())
