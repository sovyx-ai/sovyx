#!/usr/bin/env python3
"""time_helper — emite timestamp UTC (ns) + monotonic_ns pareados como JSON.

Usado pelo bash para pareamento determinístico UTC↔monotonic (§4.5 do plano).
Stdlib only. Exit code 0 sempre.

AUDIT v3 — atomicity fix
========================
A versão anterior fazia TRÊS chamadas de relógio separadas
(``datetime.now``, ``time.time_ns`` em ``_iso_ns_utc``, + outro
``time.time_ns`` em ``main`` + ``time.monotonic_ns``). A parte
fracionária do ISO podia pertencer a um segundo DIFERENTE do prefixo
(resultando em ``2026-04-22T12:34:56.999999123Z`` quando o momento
real era ``2026-04-22T12:34:57.000000123Z``). Para forensic com
precisão de ns isso é um **erro de correção**, não de performance.

A versão corrigida:
    1. Captura ``time.time_ns()`` UMA VEZ.
    2. Captura ``time.monotonic_ns()`` IMEDIATAMENTE depois, adjacente.
    3. Deriva o ISO do SAME ``time_ns`` para garantir que o prefixo
       segundo e a parte fracionária correspondem.
    4. Emite ``time_ns``, ``utc_iso_ns`` e ``monotonic_ns`` do mesmo
       instante (com drift inter-leitura < 1 µs).
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import time


def main() -> int:
    # Atomic clock pair: two adjacent reads, no intermediate work.
    # time_ns() first so utc_iso_ns derives from the EXACT same
    # nanosecond value (see module docstring for the correctness
    # rationale).
    ns = time.time_ns()
    mono = time.monotonic_ns()

    secs = ns // 1_000_000_000
    frac = ns % 1_000_000_000
    dt = _dt.datetime.fromtimestamp(secs, tz=_dt.timezone.utc)
    iso = f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{frac:09d}Z"

    payload = {
        "utc_iso_ns": iso,
        "time_ns": ns,
        "monotonic_ns": mono,
    }
    json.dump(payload, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
