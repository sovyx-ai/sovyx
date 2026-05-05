#!/usr/bin/env bash
#
# sovyx-voice-diag-bootstrap.sh
#
# Enterprise-grade installer for the Sovyx voice diagnostic toolkit.
#
# Single-command install + verify + (optionally) run, with no multi-line
# copy-paste fragility, no shell-quoting traps, and SHA256-pinned
# integrity verification against the GitHub release artifact.
#
# CANONICAL USAGE
#
#   # Install + run the full diagnostic in one shot:
#   curl -fsSL https://raw.githubusercontent.com/sovyx-ai/sovyx/main/scripts/sovyx-voice-diag-bootstrap.sh | bash -s -- --run
#
#   # Install only (inspect before running):
#   curl -fsSL https://raw.githubusercontent.com/sovyx-ai/sovyx/main/scripts/sovyx-voice-diag-bootstrap.sh | bash
#
#   # Inspect this script before piping to bash (recommended for first-time use):
#   curl -fsSL https://raw.githubusercontent.com/sovyx-ai/sovyx/main/scripts/sovyx-voice-diag-bootstrap.sh -o sovyx-voice-diag-bootstrap.sh
#   less sovyx-voice-diag-bootstrap.sh
#   bash sovyx-voice-diag-bootstrap.sh --run
#
# SUPPORTED PLATFORMS
#   Any Linux distro with bash 4+, curl, tar, sha256sum (coreutils).
#   The diagnostic itself targets Linux + PipeWire/PulseAudio.
#
# WHAT IT DOES
#   1. Verifies required tools are present (suggests install per distro if not).
#   2. Downloads the diagnostic tarball + SHA256 from a pinned GitHub release.
#   3. Verifies SHA256 against an in-script pinned hash (defense in depth).
#   4. Extracts to ~/sovyx-voice-diag-v<VERSION> (or --install-dir).
#   5. Prints next steps OR auto-runs the diagnostic with --run.
#
# NON-DESTRUCTIVE
#   No system audio config is modified. No packages are (un)installed.
#   The diagnostic stops/restarts the Sovyx daemon during its own run; this
#   bootstrap script does not touch the daemon.
#
# Licença: interno (mesmo repositório).

set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────
# Pinned release metadata — bump these together when shipping a new tarball.
# ─────────────────────────────────────────────────────────────────────────

readonly BOOTSTRAP_VERSION="1.0.0"
readonly DIAG_VERSION="4.3"
readonly RELEASE_TAG="v0.30.13"
readonly REPO="sovyx-ai/sovyx"
readonly TARBALL="sovyx-voice-diag-v${DIAG_VERSION}.tar.gz"
readonly SHA256_FILE="${TARBALL}.sha256"
readonly EXPECTED_SHA256="84a471ff694c67e0c8642f5f6861dfa889c3c28d9a533f946c7d79e039773118"
readonly EXPECTED_TOPDIR="sovyx-voice-diag-v${DIAG_VERSION}"

# ─────────────────────────────────────────────────────────────────────────
# Defaults (overridable via flags)
# ─────────────────────────────────────────────────────────────────────────

INSTALL_PARENT="${HOME}"
INSTALL_DIR=""           # computed in main() after flag parsing
RELEASE_TAG_OVERRIDE=""
RUN_DIAG=0
DOWNLOAD_ONLY=0
CHECK_ONLY=0
NO_COLOR=0

# ─────────────────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────────────────

# Default to empty so err()/info()/warn() are safe under `set -u` even before
# _init_colors() runs (e.g. when parse_args fails on an unknown flag).
RED=""; GRN=""; YLW=""; BLU=""; BLD=""; RST=""

_init_colors() {
    if (( NO_COLOR )) || [[ ! -t 1 ]] || ! command -v tput >/dev/null 2>&1; then
        return
    fi
    # tput may fail on dumb terminals; tolerate.
    RED=$(tput setaf 1 2>/dev/null || true)
    GRN=$(tput setaf 2 2>/dev/null || true)
    YLW=$(tput setaf 3 2>/dev/null || true)
    BLU=$(tput setaf 4 2>/dev/null || true)
    BLD=$(tput bold     2>/dev/null || true)
    RST=$(tput sgr0     2>/dev/null || true)
}

info() { printf "%s[i]%s %s\n"  "${BLU}" "${RST}" "$*"; }
ok()   { printf "%s[OK]%s %s\n" "${GRN}" "${RST}" "$*"; }
warn() { printf "%s[!]%s %s\n"  "${YLW}" "${RST}" "$*"; }
err()  { printf "%s[X]%s %s\n"  "${RED}" "${RST}" "$*" >&2; }

# ─────────────────────────────────────────────────────────────────────────
# Usage
# ─────────────────────────────────────────────────────────────────────────

usage() {
    cat <<EOF
Sovyx Voice Diagnostic Bootstrap v${BOOTSTRAP_VERSION}
(installs sovyx-voice-diag v${DIAG_VERSION} from release ${RELEASE_TAG})

USAGE
  curl -fsSL <bootstrap-url> | bash                    # download + verify + extract
  curl -fsSL <bootstrap-url> | bash -s -- --run        # the above + run full diag
  curl -fsSL <bootstrap-url> | bash -s -- --check      # only verify deps, no download
  bash sovyx-voice-diag-bootstrap.sh [flags]           # local invocation

FLAGS
  --run               After install, run sovyx-voice-diag.sh --yes (full diag).
                      Diag is interactive (~10 min): will ask you to speak.
  --download-only     Skip extraction (just download tarball + .sha256 to \$HOME).
  --check             Only verify required tools are present + exit (no network).
  --install-dir DIR   Override install location.
                      Default: \$HOME/${EXPECTED_TOPDIR}
  --release TAG       Override release tag (advanced; SHA256 check still applies).
                      Default: ${RELEASE_TAG}
  --no-color          Disable ANSI color output.
  --version           Print bootstrap + diag versions and exit.
  --help, -h          This message.

EXIT CODES
  0   success
  2   bad/unknown flag
  3   missing required tool (curl/tar/sha256sum/bash)
  4   download failed (network / 404 / auth)
  5   SHA256 verification failed (tarball corrupted or tampered)
  6   extraction failed (malformed tarball)
  7   diag script not found after extraction
  8   diag script returned non-zero (only with --run)

PIN INTEGRITY
  This script has a pinned SHA256 (${EXPECTED_SHA256:0:16}...)
  for ${TARBALL}. Both the in-script pin AND the .sha256 file from
  the release are verified — a tarball passing only one check is rejected.

REPRODUCIBILITY
  This bootstrap is idempotent. Re-running with the same inputs is safe.
  The install dir is overwritten on each run (after a backup warning).

REPORT ISSUES
  https://github.com/${REPO}/issues
EOF
}

# ─────────────────────────────────────────────────────────────────────────
# Argument parsing
# ─────────────────────────────────────────────────────────────────────────

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --run)            RUN_DIAG=1; shift ;;
            --download-only)  DOWNLOAD_ONLY=1; shift ;;
            --check)          CHECK_ONLY=1; shift ;;
            --install-dir)
                if [[ $# -lt 2 ]]; then err "--install-dir requires a path"; exit 2; fi
                INSTALL_DIR="$2"; shift 2
                ;;
            --release)
                if [[ $# -lt 2 ]]; then err "--release requires a tag"; exit 2; fi
                RELEASE_TAG_OVERRIDE="$2"; shift 2
                ;;
            --no-color)       NO_COLOR=1; shift ;;
            --version)
                printf "bootstrap v%s\ndiag v%s (release %s)\n" \
                    "${BOOTSTRAP_VERSION}" "${DIAG_VERSION}" "${RELEASE_TAG}"
                exit 0
                ;;
            --help|-h)        usage; exit 0 ;;
            --)               shift; break ;;
            -*)               err "unknown flag: $1"; usage >&2; exit 2 ;;
            *)                err "unexpected argument: $1"; usage >&2; exit 2 ;;
        esac
    done
}

# ─────────────────────────────────────────────────────────────────────────
# Distro detection (for friendly install hints)
# ─────────────────────────────────────────────────────────────────────────

_distro_hint() {
    local id="unknown"
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        id=$(. /etc/os-release && echo "${ID_LIKE:-$ID}")
    fi
    case "$id" in
        *debian*|*ubuntu*) echo "sudo apt update && sudo apt install -y curl tar coreutils" ;;
        *fedora*|*rhel*|*centos*) echo "sudo dnf install -y curl tar coreutils" ;;
        *arch*|*manjaro*) echo "sudo pacman -S --needed curl tar coreutils" ;;
        *suse*) echo "sudo zypper install -y curl tar coreutils" ;;
        *alpine*) echo "sudo apk add curl tar coreutils" ;;
        *) echo "(install via your distro's package manager: curl tar coreutils)" ;;
    esac
}

# ─────────────────────────────────────────────────────────────────────────
# Pre-flight: required tools
# ─────────────────────────────────────────────────────────────────────────

check_deps() {
    info "checking dependencies..."
    local missing=()
    local tool
    for tool in curl tar sha256sum mktemp; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done

    # bash 4+ check (we use mapfile-style features only optionally; require >=4 for safety)
    if (( BASH_VERSINFO[0] < 4 )); then
        err "bash 4+ required (found bash ${BASH_VERSION})"
        missing+=("bash>=4")
    fi

    if (( ${#missing[@]} > 0 )); then
        err "missing required tools: ${missing[*]}"
        err "  install: $(_distro_hint)"
        exit 3
    fi
    ok "dependencies present (bash ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}, curl, tar, sha256sum)"

    if [[ "$(uname -s)" != "Linux" ]]; then
        warn "running on $(uname -s); the diagnostic script targets Linux only"
        warn "  bootstrap will install but the diag script will refuse to run"
    fi

    if ! command -v sovyx >/dev/null 2>&1; then
        warn "sovyx CLI not found in PATH"
        warn "  the diagnostic still runs without it (uses ALSA/PipeWire tools directly),"
        warn "  but post-fix steps (sovyx doctor voice --fix) require it"
        warn "  install: pipx install sovyx   (or: pip install --user sovyx)"
    fi
}

# ─────────────────────────────────────────────────────────────────────────
# Download with retry + dual SHA256 verification
# ─────────────────────────────────────────────────────────────────────────

download_and_verify() {
    local effective_tag base_url workdir
    effective_tag="${RELEASE_TAG_OVERRIDE:-$RELEASE_TAG}"
    base_url="https://github.com/${REPO}/releases/download/${effective_tag}"

    workdir=$(mktemp -d -t sovyx-voice-diag-bootstrap.XXXXXX)
    # shellcheck disable=SC2064
    trap "rm -rf '${workdir}'" EXIT INT TERM

    info "release: ${effective_tag}"
    info "tarball: ${TARBALL}  (~150 KB)"
    info "downloading tarball..."
    if ! curl -fL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 120 \
              --progress-bar \
              -o "${workdir}/${TARBALL}" \
              "${base_url}/${TARBALL}"; then
        err "tarball download failed"
        err "  URL: ${base_url}/${TARBALL}"
        err "  troubleshooting:"
        err "    - is the GitHub release public? if private, try: gh release download ${effective_tag} --repo ${REPO} --pattern '${TARBALL}*'"
        err "    - network reachable? curl -v https://github.com 2>&1 | head -20"
        err "    - corp proxy? set HTTPS_PROXY environment variable"
        exit 4
    fi

    info "downloading sidecar SHA256..."
    if ! curl -fL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 30 \
              -sS \
              -o "${workdir}/${SHA256_FILE}" \
              "${base_url}/${SHA256_FILE}"; then
        err "sidecar SHA256 download failed (URL: ${base_url}/${SHA256_FILE})"
        exit 4
    fi

    info "verifying sidecar SHA256..."
    if ! ( cd "${workdir}" && sha256sum -c "${SHA256_FILE}" >/dev/null 2>&1 ); then
        err "sidecar SHA256 verification FAILED"
        err "  the tarball does not match its published .sha256 sidecar"
        err "  expected (sidecar): $(cat "${workdir}/${SHA256_FILE}")"
        err "  actual:             $(sha256sum "${workdir}/${TARBALL}")"
        err "  do NOT extract or run; report this immediately"
        exit 5
    fi

    info "verifying pinned SHA256 (defense in depth)..."
    local actual
    actual=$(sha256sum "${workdir}/${TARBALL}" | awk '{print $1}')
    if [[ "$actual" != "$EXPECTED_SHA256" ]]; then
        # If --release was overridden, the pin won't match by design — warn and skip.
        if [[ -n "$RELEASE_TAG_OVERRIDE" ]]; then
            warn "pinned SHA256 mismatch (--release override in use)"
            warn "  expected (pinned): ${EXPECTED_SHA256}"
            warn "  actual (downloaded): ${actual}"
            warn "  proceeding because --release was explicitly overridden"
        else
            err "pinned SHA256 verification FAILED"
            err "  expected: ${EXPECTED_SHA256}"
            err "  actual:   ${actual}"
            err "  this should NEVER happen — bootstrap version mismatch with release"
            err "  do NOT extract or run; please report this incident"
            exit 5
        fi
    fi
    ok "SHA256 verified (sidecar + pinned)"

    # Stage to $HOME atomically.
    mv "${workdir}/${TARBALL}"     "${INSTALL_PARENT}/${TARBALL}"
    mv "${workdir}/${SHA256_FILE}" "${INSTALL_PARENT}/${SHA256_FILE}"
    ok "downloaded: ${INSTALL_PARENT}/${TARBALL}"

    # Trap will clean up workdir.
}

# ─────────────────────────────────────────────────────────────────────────
# Extract + validate structure
# ─────────────────────────────────────────────────────────────────────────

extract() {
    local target="${INSTALL_DIR}"

    if [[ -e "${target}" ]]; then
        if [[ -d "${target}" ]]; then
            warn "${target} already exists — replacing"
            rm -rf "${target}"
        else
            err "${target} exists but is not a directory; refusing to overwrite"
            exit 6
        fi
    fi

    info "extracting to ${INSTALL_PARENT}/..."
    if ! ( cd "${INSTALL_PARENT}" && tar xzf "${TARBALL}" ); then
        err "tar extraction failed"
        exit 6
    fi

    # Validate expected layout.
    local script="${INSTALL_PARENT}/${EXPECTED_TOPDIR}/sovyx-voice-diag.sh"
    if [[ ! -f "${script}" ]]; then
        err "sovyx-voice-diag.sh not found at expected path"
        err "  expected: ${script}"
        err "  tarball structure may have changed; please report this"
        exit 7
    fi
    chmod +x "${script}"

    # If user asked for a non-default install dir, move into place.
    if [[ "${target}" != "${INSTALL_PARENT}/${EXPECTED_TOPDIR}" ]]; then
        mv "${INSTALL_PARENT}/${EXPECTED_TOPDIR}" "${target}"
    fi

    ok "installed: ${target}/sovyx-voice-diag.sh"
}

# ─────────────────────────────────────────────────────────────────────────
# Run diagnostic
# ─────────────────────────────────────────────────────────────────────────

run_diag() {
    local script="${INSTALL_DIR}/sovyx-voice-diag.sh"
    printf "\n%s================================================================%s\n" "${BLD}" "${RST}"
    printf   "%s  Starting full voice diagnostic (8-12 min, interactive)%s\n"          "${BLD}" "${RST}"
    printf   "%s  You will be asked to speak in short windows. Stay near the mic.%s\n" "${BLD}" "${RST}"
    printf   "%s================================================================%s\n\n" "${BLD}" "${RST}"

    set +e
    bash "${script}" --yes
    local rc=$?
    set -e

    if (( rc != 0 )); then
        err "diagnostic exited with code ${rc}"
        err "  see ${INSTALL_DIR} for partial outputs"
        exit 8
    fi
    ok "diagnostic completed"
    print_post_run_hints
}

# ─────────────────────────────────────────────────────────────────────────
# Post-run / next-steps banner
# ─────────────────────────────────────────────────────────────────────────

print_next_steps() {
    cat <<EOF

${BLD}NEXT STEPS${RST}

  1) Run the full diagnostic (8-12 min, interactive — will ask you to speak):

       bash ${INSTALL_DIR}/sovyx-voice-diag.sh --yes

     Or list all flags:

       bash ${INSTALL_DIR}/sovyx-voice-diag.sh --help

  2) When it finishes, the result tarball will be at:

       ~/sovyx-diag-<host>-<ts>-<uuid>/sovyx-voice-diag_*.tar.gz

  3) Send that path back for triage. To find the most recent one:

       ls -1t ~/sovyx-diag-*/sovyx-voice-diag_*.tar.gz | head -1

EOF
}

print_post_run_hints() {
    cat <<EOF

${BLD}RESULT TARBALL${RST}

  Most recent diagnostic tarball:

    ls -1t ~/sovyx-diag-*/sovyx-voice-diag_*.tar.gz | head -1

  Send that path + the script's exit summary for triage analysis.

EOF
}

# ─────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────

main() {
    parse_args "$@"
    _init_colors

    printf "%sSovyx Voice Diagnostic Bootstrap v%s%s\n" "${BLD}" "${BOOTSTRAP_VERSION}" "${RST}"
    printf "%s(installs sovyx-voice-diag v%s from release %s)%s\n\n" \
        "${BLD}" "${DIAG_VERSION}" "${RELEASE_TAG_OVERRIDE:-$RELEASE_TAG}" "${RST}"

    # Compute INSTALL_DIR after flag parsing so --install-dir wins.
    if [[ -z "${INSTALL_DIR}" ]]; then
        INSTALL_DIR="${INSTALL_PARENT}/${EXPECTED_TOPDIR}"
    fi

    check_deps

    if (( CHECK_ONLY )); then
        ok "check-only mode: dependencies OK, exiting"
        exit 0
    fi

    download_and_verify

    if (( DOWNLOAD_ONLY )); then
        ok "download complete (--download-only); skipping extraction"
        info "to extract later: tar xzf ${INSTALL_PARENT}/${TARBALL} -C ${INSTALL_PARENT}"
        exit 0
    fi

    extract

    if (( RUN_DIAG )); then
        run_diag
    else
        print_next_steps
    fi
}

main "$@"
