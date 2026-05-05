#!/usr/bin/env python3
"""wav_header — dump do cabeçalho RIFF de um arquivo WAV.

Usado por analyze_wav.py para anexar metadados fiéis ao analysis.json.
Stdlib only.

Uso:
    python3 wav_header.py <file.wav>

Saída: JSON em stdout com campos do chunk fmt + data.
Exit code: 0 ok, 1 falha de leitura, 2 não é WAV.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path


def parse_wav_header(path: Path) -> dict[str, object]:
    with path.open("rb") as f:
        header = f.read(12)
        if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
            return {"error": "not_a_wav", "first_bytes_hex": header.hex()}

        result: dict[str, object] = {
            "riff_size": struct.unpack("<I", header[4:8])[0],
            "format": "WAVE",
            "chunks": [],
        }

        while True:
            chunk_hdr = f.read(8)
            if len(chunk_hdr) < 8:
                break
            chunk_id = chunk_hdr[0:4].decode("ascii", errors="replace")
            chunk_size = struct.unpack("<I", chunk_hdr[4:8])[0]
            chunk_info: dict[str, object] = {"id": chunk_id, "size": chunk_size}

            if chunk_id == "fmt ":
                fmt_data = f.read(min(chunk_size, 16))
                if len(fmt_data) >= 16:
                    (
                        audio_format,
                        channels,
                        sample_rate,
                        byte_rate,
                        block_align,
                        bits_per_sample,
                    ) = struct.unpack("<HHIIHH", fmt_data[:16])
                    fmt_names = {
                        1: "PCM",
                        3: "IEEE_FLOAT",
                        6: "ALAW",
                        7: "ULAW",
                        0xFFFE: "EXTENSIBLE",
                    }
                    chunk_info.update(
                        {
                            "audio_format": fmt_names.get(audio_format, f"unknown({audio_format})"),
                            "audio_format_code": audio_format,
                            "channels": channels,
                            "sample_rate": sample_rate,
                            "byte_rate": byte_rate,
                            "block_align": block_align,
                            "bits_per_sample": bits_per_sample,
                        }
                    )
                # Skip any extra fmt bytes.
                if chunk_size > 16:
                    f.seek(chunk_size - 16, 1)
            elif chunk_id == "data":
                chunk_info["data_start_offset"] = f.tell()
                chunk_info["data_bytes"] = chunk_size
                # Don't read data; just skip.
                f.seek(chunk_size, 1)
            else:
                # Unknown chunk — skip.
                f.seek(chunk_size, 1)

            # Chunks are word-aligned.
            if chunk_size % 2 == 1:
                f.seek(1, 1)

            result["chunks"].append(chunk_info)

    return result


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: wav_header.py <file.wav>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print(json.dumps({"error": "file_not_found", "path": str(path)}))
        return 1
    try:
        info = parse_wav_header(path)
    except Exception as exc:  # noqa: BLE001
        info = {"error": "parse_failed", "detail": str(exc)}
    info["file"] = str(path)
    info["file_size_bytes"] = path.stat().st_size
    json.dump(info, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if "error" not in info else 1


if __name__ == "__main__":
    sys.exit(main())
