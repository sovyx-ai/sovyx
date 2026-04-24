# Sovyx packaging

System-level artifacts that ship with the Sovyx release and need to
land outside the Python package directory: systemd units, udev rules,
and helper scripts invoked with elevated capabilities.

## Contents

```
packaging/
├── systemd/
│   ├── sovyx-audio-runtime-pm.service
│   └── audio-runtime-pm-setup
└── udev/
    └── 60-sovyx-audio-power.rules
```

## What these do

Together they keep PCI audio-class devices at `runtime_pm=on` so
Sovyx's voice capture pipeline doesn't suffer the ~500 ms D0 wake
that truncates the first phrase of every utterance on affected
laptops (pilot case: Sony VAIO VJFE69F11X with Conexant SN6180 —
`SVX-VOICE-LINUX-VJFE69-20260423`). This is the runtime_pm half of
the L2.5 mixer-sanity story; the ALSA mixer half lives in the
Python package (`src/sovyx/voice/health/_mixer_sanity.py`).

**The daemon never touches `/sys/bus/pci/**/power/control` at
runtime** (invariant I7). All writes happen:

1. At boot via `sovyx-audio-runtime-pm.service` (systemd oneshot).
2. On udev hotplug / resume via `60-sovyx-audio-power.rules`.

## Install paths

| File | Destination |
|---|---|
| `sovyx-audio-runtime-pm.service` | `/etc/systemd/system/sovyx-audio-runtime-pm.service` (or `/usr/lib/systemd/system/` for vendor installs) |
| `audio-runtime-pm-setup` | `/usr/libexec/sovyx/audio-runtime-pm-setup` (must be executable, `0755 root:root`) |
| `60-sovyx-audio-power.rules` | `/etc/udev/rules.d/60-sovyx-audio-power.rules` (or `/usr/lib/udev/rules.d/`) |

## Package integration

Distro-specific post-install hooks should run:

```sh
systemctl daemon-reload
systemctl enable --now sovyx-audio-runtime-pm.service
udevadm control --reload-rules
udevadm trigger --action=change --subsystem-match=sound
```

And on uninstall:

```sh
systemctl disable --now sovyx-audio-runtime-pm.service
rm -f /etc/systemd/system/sovyx-audio-runtime-pm.service \
      /usr/libexec/sovyx/audio-runtime-pm-setup \
      /etc/udev/rules.d/60-sovyx-audio-power.rules
systemctl daemon-reload
udevadm control --reload-rules
```

No residual state in `/sys` after uninstall — on next boot without
the service, the kernel default runtime_pm behaviour resumes.

### Sandboxed install (snap / Flatpak)

These sandboxes cannot install system-level systemd units. Sovyx
running under snap / Flatpak logs a WARN on first boot explaining
that codec runtime_pm is not controllable in the sandbox, and falls
back to the DSP AGC path. Voice still works; the first-phrase
trickle may recur on affected hardware until the user installs the
native .deb / .rpm alongside.

### pipx / user-scope installs

`pipx install sovyx` lacks automatic system integration. Users
running Sovyx via pipx should copy these files manually (same paths
as above) and run the post-install commands with `sudo`. A
`postinstall_admin.sh` helper is planned for v0.23.0.

## Operator escape hatch

The systemd unit has
`ConditionKernelCommandLine=!sovyx.audio.no_pm_override`. Adding
`sovyx.audio.no_pm_override` to the kernel command line (via
`/etc/default/grub` + `update-grub`) disables the whole unit — for
battery-critical embedded Linux deployments where the user
explicitly prefers the D3 idle power saving over the first-phrase
latency trade.

## Validation

On a target Linux machine, after install:

```sh
systemctl status sovyx-audio-runtime-pm.service
# Should show "active (exited)" within ~1s of boot.

for f in /sys/bus/pci/devices/*/class; do
    class=$(cat "$f")
    case "$class" in
        0x0403*)
            power=$(dirname "$f")/power/control
            echo "$(dirname "$f"): $class → $(cat "$power")"
            ;;
    esac
done
# Every audio-class device should report "on".

sudo udevadm test /sys/class/sound/card0 2>&1 | grep sovyx
# Should show the rule firing on the sound subsystem event.
```

## References

- ADR: `docs-internal/ADR-voice-mixer-sanity-l2.5-bidirectional.md`
  (§2 architecture, §H packaging spec, §I security model).
- V2 Master Plan: `docs-internal/missions/VOICE-MIXER-SANITY-L2.5-MASTER-PLAN-v2.md`
  Part H.
- Forensic root cause: `docs-internal/MISSION-VOICE-LINUX-VJFE69-RCA-20260423.md`.
