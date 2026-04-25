#!/usr/bin/env bash
# hal-plugin-classifier — enumera HAL plug-ins, AU components e system
# extensions de áudio em macOS. Classifica em vendors conhecidos
# (BlackHole, Loopback, Audio Hijack, Krisp, etc.) — análogo macOS do
# APO catalog do Windows.
#
# Output JSON em stdout.
#
# Uso:
#   bash hal-plugin-classifier.sh > hal_classifier.json

set -uo pipefail

TOOL_VERSION="1.0"

# ─────────────────────────────────────────────────────────────────────
# Vendor catalog — substring patterns matched against bundle name +
# bundle id + signing subject.
# ─────────────────────────────────────────────────────────────────────
declare -A VENDOR_PATTERNS=(
    ["blackhole"]="BlackHole virtual audio (Existential Audio)"
    ["existentialaudio"]="Existential Audio (BlackHole vendor)"
    ["loopback"]="Loopback (Rogue Amoeba)"
    ["rogueamoeba"]="Rogue Amoeba (Loopback/Audio Hijack/SoundSource)"
    ["audiohijack"]="Audio Hijack (Rogue Amoeba)"
    ["soundsource"]="SoundSource (Rogue Amoeba)"
    ["soundflower"]="Soundflower (Cycling 74 — discontinued)"
    ["krisp"]="Krisp noise suppression (analog of Voice Clarity APO)"
    ["nvidia"]="NVIDIA Broadcast (Mac version: rare/unsupported)"
    ["voicemeeter"]="VoiceMeeter (no native Mac support)"
    ["voicemod"]="Voicemod"
    ["zoom"]="Zoom audio plugin"
    ["discord"]="Discord audio plugin"
    ["teams"]="Microsoft Teams audio plugin"
    ["webex"]="Cisco Webex audio plugin"
    ["nahimic"]="Nahimic audio enhancement"
    ["waves"]="Waves MaxxAudio"
    ["dolby"]="Dolby Atmos / DAX"
    ["sonicstudio"]="ASUS Sonic Studio"
)

emit_array_start() { printf '['; }
emit_array_end() { printf ']'; }

# Classify a bundle by inspecting its path + Info.plist + codesign.
classify_bundle() {
    local path="$1"
    local name=""
    local bundle_id=""
    local team_id=""
    local signing_subject=""
    local exec_arch=""
    local matched_vendor=""
    local matched_keyword=""

    name=$(basename "$path")

    # Read CFBundleIdentifier from Info.plist if present.
    local info_plist="$path/Contents/Info.plist"
    if [[ -r "$info_plist" ]]; then
        bundle_id=$(defaults read "$info_plist" CFBundleIdentifier 2>/dev/null || echo "")
    fi

    # codesign signing info.
    local cs_out
    cs_out=$(codesign -dvvv "$path" 2>&1 || true)
    team_id=$(printf '%s' "$cs_out" | awk -F'TeamIdentifier=' '/TeamIdentifier=/{print $2; exit}' | awk '{print $1}')
    signing_subject=$(printf '%s' "$cs_out" | awk -F'Authority=' '/Authority=/{print $2; exit}')

    # lipo for arch (DEXT/HAL plugin executable).
    local exec_path="$path/Contents/MacOS/$(basename "$path" .driver)"
    if [[ ! -f "$exec_path" ]]; then
        # Try first file in MacOS dir.
        exec_path=$(find "$path/Contents/MacOS" -maxdepth 1 -type f 2>/dev/null | head -1)
    fi
    if [[ -n "$exec_path" && -f "$exec_path" ]]; then
        exec_arch=$(lipo -info "$exec_path" 2>/dev/null | sed 's/.*are: //;s/.*is architecture: //')
    fi

    # Match against vendor patterns.
    local search_str
    search_str=$(printf '%s %s %s' "$name" "$bundle_id" "$signing_subject" \
                 | tr '[:upper:]' '[:lower:]')
    for kw in "${!VENDOR_PATTERNS[@]}"; do
        if [[ "$search_str" == *"$kw"* ]]; then
            matched_vendor="${VENDOR_PATTERNS[$kw]}"
            matched_keyword="$kw"
            break
        fi
    done

    # JSON output via python.
    python3 -c "
import json, sys
d = {
    'path': sys.argv[1],
    'name': sys.argv[2],
    'bundle_id': sys.argv[3],
    'team_id': sys.argv[4],
    'signing_subject': sys.argv[5],
    'exec_arch': sys.argv[6],
    'matched_vendor': sys.argv[7],
    'matched_keyword': sys.argv[8],
}
print(json.dumps(d))
" "$path" "$name" "$bundle_id" "$team_id" "$signing_subject" "$exec_arch" "$matched_vendor" "$matched_keyword"
}

# ─────────────────────────────────────────────────────────────────────
# Walk HAL + AU + system extensions
# ─────────────────────────────────────────────────────────────────────

# Use python3 to assemble final JSON (avoids JSON escaping pain in bash).
python3 - <<'PYEOF' "$TOOL_VERSION"
import json, os, subprocess, sys, time
from pathlib import Path

tool_version = sys.argv[1]
out = {
    "ok": True,
    "tool_version": tool_version,
    "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "hal_plugins": [],
    "au_components": [],
    "system_extensions": [],
    "errors": [],
}

hal_dirs = [
    Path("/Library/Audio/Plug-Ins/HAL"),
    Path.home() / "Library" / "Audio" / "Plug-Ins" / "HAL",
]
au_dirs = [
    Path("/Library/Audio/Plug-Ins/Components"),
    Path.home() / "Library" / "Audio" / "Plug-Ins" / "Components",
]

def call_classifier(plugin_path):
    """Invoke the bash classify_bundle helper for one plugin path."""
    try:
        res = subprocess.run(
            ["bash", "-c",
             f"source '{__file__}' >/dev/null 2>&1; classify_bundle '{plugin_path}'"],
            capture_output=True, text=True, timeout=30,
        )
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout.strip().splitlines()[-1])
    except Exception as e:
        return {"path": str(plugin_path), "error": str(e)}
    return {"path": str(plugin_path), "error": "classify_failed"}

# Note: Python invoking bash invoking python is awkward. Simpler: do
# the classification in-line in Python.
def classify_inline(plugin_path):
    p = Path(plugin_path)
    info = {
        "path": str(p),
        "name": p.name,
        "bundle_id": "",
        "team_id": "",
        "signing_subject": "",
        "exec_arch": "",
        "matched_vendor": "",
        "matched_keyword": "",
    }
    info_plist = p / "Contents" / "Info.plist"
    if info_plist.is_file():
        try:
            res = subprocess.run(
                ["defaults", "read", str(info_plist), "CFBundleIdentifier"],
                capture_output=True, text=True, timeout=5,
            )
            if res.returncode == 0:
                info["bundle_id"] = res.stdout.strip()
        except Exception:
            pass
    try:
        res = subprocess.run(
            ["codesign", "-dvvv", str(p)],
            capture_output=True, text=True, timeout=10,
        )
        cs_out = res.stderr + res.stdout
        for line in cs_out.splitlines():
            if line.startswith("TeamIdentifier="):
                info["team_id"] = line.split("=", 1)[1].strip()
            elif line.startswith("Authority=") and not info["signing_subject"]:
                info["signing_subject"] = line.split("=", 1)[1].strip()
    except Exception:
        pass
    macos_dir = p / "Contents" / "MacOS"
    if macos_dir.is_dir():
        execs = list(macos_dir.iterdir())
        if execs:
            try:
                res = subprocess.run(
                    ["lipo", "-info", str(execs[0])],
                    capture_output=True, text=True, timeout=5,
                )
                if res.returncode == 0:
                    line = res.stdout.strip()
                    for marker in ["are: ", "is architecture: "]:
                        if marker in line:
                            info["exec_arch"] = line.split(marker, 1)[1]
                            break
            except Exception:
                pass

    vendors = {
        "blackhole": "BlackHole virtual audio (Existential Audio)",
        "existentialaudio": "Existential Audio (BlackHole vendor)",
        "loopback": "Loopback (Rogue Amoeba)",
        "rogueamoeba": "Rogue Amoeba (Loopback/Audio Hijack/SoundSource)",
        "audiohijack": "Audio Hijack (Rogue Amoeba)",
        "soundsource": "SoundSource (Rogue Amoeba)",
        "soundflower": "Soundflower (Cycling 74 -- discontinued)",
        "krisp": "Krisp noise suppression (analog of Voice Clarity APO)",
        "nvidia": "NVIDIA Broadcast (Mac version: rare/unsupported)",
        "voicemeeter": "VoiceMeeter (no native Mac support)",
        "voicemod": "Voicemod",
        "zoom": "Zoom audio plugin",
        "discord": "Discord audio plugin",
        "teams": "Microsoft Teams audio plugin",
        "webex": "Cisco Webex audio plugin",
        "nahimic": "Nahimic audio enhancement",
        "waves": "Waves MaxxAudio",
        "dolby": "Dolby Atmos / DAX",
        "sonicstudio": "ASUS Sonic Studio",
    }
    haystack = f"{info['name']} {info['bundle_id']} {info['signing_subject']}".lower()
    for kw, label in vendors.items():
        if kw in haystack:
            info["matched_vendor"] = label
            info["matched_keyword"] = kw
            break
    return info

for d in hal_dirs:
    if not d.is_dir():
        continue
    for entry in d.iterdir():
        if entry.suffix == ".driver" or entry.is_dir():
            out["hal_plugins"].append(classify_inline(entry))

for d in au_dirs:
    if not d.is_dir():
        continue
    for entry in d.iterdir():
        if entry.suffix in (".component", ".audiocomp") or entry.is_dir():
            out["au_components"].append(classify_inline(entry))

# system extensions (DEXTs).
try:
    res = subprocess.run(
        ["systemextensionsctl", "list"],
        capture_output=True, text=True, timeout=15,
    )
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            line_stripped = line.strip()
            if not line_stripped or line_stripped.startswith("---"):
                continue
            # Audio-related entries: rough heuristic on substrings.
            lc = line_stripped.lower()
            if any(kw in lc for kw in ["audio", "blackhole", "krisp", "loopback",
                                        "rogueamoeba", "existential"]):
                out["system_extensions"].append({"raw_line": line_stripped})
except Exception as e:
    out["errors"].append(f"systemextensionsctl_failed: {e}")

# Heuristic alert: if any HAL plugin matches a known interceptor and
# is the macOS analog of Voice Clarity APO.
out["interceptors_detected"] = [
    h for h in out["hal_plugins"] + out["au_components"]
    if h.get("matched_vendor")
]

print(json.dumps(out, indent=2))
PYEOF
