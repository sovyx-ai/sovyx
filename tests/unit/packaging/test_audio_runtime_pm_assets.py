"""Regression tests for the ``packaging/`` Linux artifacts.

Text-level invariant assertions — these files are Linux-only
(systemd unit, POSIX sh script, udev rule) and cannot be semantically
validated on Windows/macOS without ``systemd-analyze`` /
``udevadm verify`` / ``shellcheck``. We focus on catching common
regressions that would break the file on a real Linux box:

* shell script is a plain ``#!/bin/sh`` (not bash-only)
* ``set -eu`` is present (fail-fast on unbound variable / error)
* class-code glob matches ``0x0403*`` (PCI audio-class spec)
* systemd unit declares ``Type=oneshot`` + ``NoNewPrivileges=yes``
* udev rule uses ``SUBSYSTEM=="sound"`` + ``ATTRS{class}=="0x040300"``
* no trailing ``sudo`` / ``polkit`` invocations (invariant I7)

Every assertion has a regression story — if one fails, the
corresponding invariant was broken by a later edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SYSTEMD_UNIT = _REPO_ROOT / "packaging" / "systemd" / "sovyx-audio-runtime-pm.service"
_SH_HELPER = _REPO_ROOT / "packaging" / "systemd" / "audio-runtime-pm-setup"
_PERSIST_UNIT = _REPO_ROOT / "packaging" / "systemd" / "sovyx-audio-mixer-persist.service"
_UDEV_RULE = _REPO_ROOT / "packaging" / "udev" / "60-sovyx-audio-power.rules"


def _strip_shell_comments(text: str) -> str:
    """Replace any comment-only line with an empty line.

    Used by the ``no sudo / polkit / sh PATH`` invariant tests so a
    legitimate commentary mention (e.g., "zero sudo/polkit exposure")
    doesn't false-positive the regression guard. The guard is only
    meaningful for directive lines — the ``ACTION="..."`` /
    ``RUN+="..."`` / shell command lines.
    """
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            out.append("")
        else:
            out.append(line)
    return "\n".join(out)


# ── Asset presence ──────────────────────────────────────────────────


class TestAssetsExist:
    def test_systemd_unit_present(self) -> None:
        assert _SYSTEMD_UNIT.is_file()

    def test_shell_helper_present(self) -> None:
        assert _SH_HELPER.is_file()

    def test_udev_rule_present(self) -> None:
        assert _UDEV_RULE.is_file()

    def test_persist_unit_present(self) -> None:
        assert _PERSIST_UNIT.is_file()


# ── systemd unit invariants ─────────────────────────────────────────


class TestSystemdUnit:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _SYSTEMD_UNIT.read_text(encoding="utf-8")

    def test_has_unit_section(self, text: str) -> None:
        assert "[Unit]" in text

    def test_has_service_section(self, text: str) -> None:
        assert "[Service]" in text

    def test_has_install_section(self, text: str) -> None:
        assert "[Install]" in text

    def test_is_oneshot(self, text: str) -> None:
        assert "Type=oneshot" in text

    def test_remain_after_exit(self, text: str) -> None:
        """oneshot + RemainAfterExit=yes is the canonical pattern for
        set-once-at-boot units. Without this, `systemctl status` shows
        the unit as inactive (dead) after the exec finishes."""
        assert "RemainAfterExit=yes" in text

    def test_no_new_privileges(self, text: str) -> None:
        """Invariant I7 — the unit runs as root but drops all extra
        privileges. Breaking this expands the attack surface of the
        helper script."""
        assert "NoNewPrivileges=yes" in text

    def test_empty_capability_bounding_set(self, text: str) -> None:
        """root + CapabilityBoundingSet= (empty) drops every
        ambient capability; only the file-write privilege from
        ReadWritePaths remains."""
        assert "CapabilityBoundingSet=" in text

    def test_protect_system_strict(self, text: str) -> None:
        assert "ProtectSystem=strict" in text

    def test_read_write_paths_scoped_to_pci_devices(self, text: str) -> None:
        """Only /sys/bus/pci/devices may be written — /sys/bus/pci,
        /proc, /etc are read-only under ProtectSystem=strict."""
        assert "ReadWritePaths=/sys/bus/pci/devices" in text

    def test_protect_kernel_tunables(self, text: str) -> None:
        assert "ProtectKernelTunables=yes" in text

    def test_escape_hatch_on_kernel_cmdline(self, text: str) -> None:
        """Operators can disable the unit globally via kernel cmdline."""
        assert "ConditionKernelCommandLine=!sovyx.audio.no_pm_override" in text

    def test_sound_presence_gate(self, text: str) -> None:
        """Don't run on sound-less containers."""
        assert "ConditionPathExists=/sys/class/sound" in text

    def test_exec_start_points_to_helper(self, text: str) -> None:
        assert "ExecStart=/usr/libexec/sovyx/audio-runtime-pm-setup" in text

    def test_wanted_by_multi_user_target(self, text: str) -> None:
        assert "WantedBy=multi-user.target" in text

    def test_no_sudo_or_polkit_reference(self, text: str) -> None:
        """Invariant I7 — zero daemon-time privilege escalation
        surface. A `sudo` or `pkexec` directive would be a regression
        on that boundary. Comments are stripped so commentary mentions
        don't false-positive.
        """
        directives = _strip_shell_comments(text).lower()
        assert "sudo" not in directives
        assert "pkexec" not in directives
        assert "polkit" not in directives


# ── Shell helper invariants ─────────────────────────────────────────


class TestShellHelper:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _SH_HELPER.read_text(encoding="utf-8")

    def test_posix_sh_shebang(self, text: str) -> None:
        """#!/bin/sh — NOT #!/bin/bash. Some distros (Alpine,
        BusyBox-based embedded) don't ship bash by default."""
        first_line = text.splitlines()[0]
        assert first_line == "#!/bin/sh"

    def test_set_eu(self, text: str) -> None:
        """set -eu — fail-fast on unbound variable / non-zero exit."""
        assert "set -eu" in text

    def test_matches_audio_class_pci(self, text: str) -> None:
        """Class-code glob must catch HDA + AC'97 + audio bridges."""
        assert "0x0403*" in text

    def test_targets_power_control_file(self, text: str) -> None:
        assert "power/control" in text

    def test_writes_on(self, text: str) -> None:
        assert "echo on" in text

    def test_exit_zero(self, text: str) -> None:
        """Must never fail the systemd unit — a partially masked
        /sys (container) is not a real failure."""
        assert "exit 0" in text

    def test_no_bashisms(self, text: str) -> None:
        """Common bashisms that would break under dash / BusyBox."""
        assert "[[ " not in text  # bash extended test
        assert "function " not in text  # bash function keyword
        assert " == " not in text  # bash equality (POSIX sh uses =)

    def test_idempotent_guard(self, text: str) -> None:
        """Current-state check prevents spurious writes on every
        boot / udev event — idempotency invariant."""
        assert 'if [ "$current" != "on" ]' in text

    def test_writable_guard(self, text: str) -> None:
        """Gracefully skip devices without runtime_pm support."""
        assert '[ -w "$power_ctl" ]' in text

    def test_no_curl_wget_nc(self, text: str) -> None:
        """No network in a post-boot systemd oneshot (invariant I7
        + security principle — zero network surface). Comments
        stripped so commentary mentions don't false-positive.
        """
        directives = _strip_shell_comments(text).lower()
        assert "curl" not in directives
        assert "wget" not in directives
        assert "nc " not in directives  # netcat
        assert "netcat" not in directives

    def test_short_enough(self, text: str) -> None:
        """Helper is spec'd as ~40 LOC; drift past 100 LOC means
        the scope crept. Force a review."""
        lines = text.splitlines()
        assert len(lines) <= 100, (
            f"helper is {len(lines)} LOC — split or simplify; V2 spec targets ~40 LOC"
        )


# ── udev rule invariants ────────────────────────────────────────────


class TestUdevRule:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _UDEV_RULE.read_text(encoding="utf-8")

    def test_narrow_subsystem(self, text: str) -> None:
        """Must only fire on sound-subsystem events — a broader rule
        would run on every PCI change and waste cycles."""
        assert 'SUBSYSTEM=="sound"' in text

    def test_class_code_filter(self, text: str) -> None:
        assert 'ATTRS{class}=="0x040300"' in text

    def test_handles_add_and_change(self, text: str) -> None:
        """Two rules: add (codec driver bind) + change (hotplug /
        resume). Missing either would leave a gap."""
        assert 'ACTION=="add"' in text
        assert 'ACTION=="change"' in text

    def test_writes_on(self, text: str) -> None:
        assert "echo on" in text

    def test_writable_guard_inline(self, text: str) -> None:
        """test -w before writing — skip devices without runtime_pm."""
        assert "test -w" in text

    def test_uses_device_scoped_sysfs(self, text: str) -> None:
        """%S%p expands to the absolute sysfs path of the device —
        device-scoped, no enumeration loop."""
        assert "%S%p" in text

    def test_no_external_binary_invocation_beyond_sh(self, text: str) -> None:
        """Udev rules run with a narrow PATH — invoking anything
        outside /bin/sh risks broken distros (Alpine puts /bin in
        /usr/bin via symlink, etc.)."""
        directives = _strip_shell_comments(text).lower()
        assert "/usr/bin/" not in directives
        assert "/usr/local/bin" not in directives

    def test_no_sudo_or_pkexec(self, text: str) -> None:
        """Invariant I7 in the udev context too. Comments stripped
        so commentary mentions don't false-positive.
        """
        directives = _strip_shell_comments(text).lower()
        assert "sudo" not in directives
        assert "pkexec" not in directives


# ── Mixer persist unit invariants ───────────────────────────────────


class TestMixerPersistUnit:
    """``sovyx-audio-mixer-persist.service`` — L2.5 post-heal ``alsactl
    store`` delegate. NOT enabled at boot; triggered on-demand by the
    daemon via ``systemctl start --no-block``.
    """

    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _PERSIST_UNIT.read_text(encoding="utf-8")

    def test_has_unit_section(self, text: str) -> None:
        assert "[Unit]" in text

    def test_has_service_section(self, text: str) -> None:
        assert "[Service]" in text

    def test_is_oneshot(self, text: str) -> None:
        assert "Type=oneshot" in text

    def test_execs_alsactl_store(self, text: str) -> None:
        assert "ExecStart=/usr/sbin/alsactl store -f" in text

    def test_no_install_section(self, text: str) -> None:
        """NOT enabled at boot — triggered on-demand via
        ``systemctl start``. An ``[Install]`` block with
        ``WantedBy=`` would auto-enable it on package install, which
        we explicitly don't want. Comments stripped so commentary
        mentions don't false-positive.
        """
        directives = _strip_shell_comments(text)
        assert "[Install]" not in directives
        assert "WantedBy=" not in directives

    def test_read_write_paths_scoped(self, text: str) -> None:
        """Only ``/var/lib/alsa`` may be written — everything else
        read-only under ``ProtectSystem=strict``.
        """
        assert "ReadWritePaths=/var/lib/alsa" in text

    def test_no_new_privileges(self, text: str) -> None:
        assert "NoNewPrivileges=yes" in text

    def test_empty_capability_bounding_set(self, text: str) -> None:
        assert "CapabilityBoundingSet=" in text

    def test_protect_system_strict(self, text: str) -> None:
        assert "ProtectSystem=strict" in text

    def test_timeout_bounded(self, text: str) -> None:
        """TimeoutStartSec caps the wall-clock so a pathological
        /var/lib/alsa never blocks the L2.5 orchestrator's
        systemctl-start call.
        """
        assert "TimeoutStartSec=" in text

    def test_condition_path_exists(self, text: str) -> None:
        """Don't run on systems without /var/lib/alsa (rare — nosystemd minimal containers)."""
        assert "ConditionPathExists=/var/lib/alsa" in text

    def test_no_sudo_or_polkit(self, text: str) -> None:
        directives = _strip_shell_comments(text).lower()
        assert "sudo" not in directives
        assert "pkexec" not in directives
        assert "polkit" not in directives
