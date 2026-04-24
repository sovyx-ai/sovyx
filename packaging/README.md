# Sovyx packaging

System-level artifacts that ship with the Sovyx release and need to
land outside the Python package directory: systemd units, udev rules,
and helper scripts invoked with elevated capabilities.

## Contents

```
packaging/
├── systemd/
│   ├── sovyx-audio-runtime-pm.service    (boot oneshot — runtime_pm)
│   ├── audio-runtime-pm-setup            (POSIX sh helper for the above)
│   └── sovyx-audio-mixer-persist.service (on-demand — alsactl store)
└── udev/
    └── 60-sovyx-audio-power.rules
```

## What these do

Together they let L2.5 keep the ALSA capture path healthy without
granting the Sovyx daemon any extra OS privileges (invariant I7 —
zero daemon-time privilege escalation).

**Two independent concerns**:

1. **runtime_pm** — keep PCI audio-class devices at `power/control=on`
   so Sovyx's voice capture doesn't suffer the ~500 ms D0 wake that
   truncates the first phrase of every utterance on affected laptops
   (pilot case: Sony VAIO VJFE69F11X with Conexant SN6180 —
   `SVX-VOICE-LINUX-VJFE69-20260423`). Handled by
   `sovyx-audio-runtime-pm.service` at boot and the udev rule on
   hotplug / resume / codec rebind.
2. **Mixer-state persistence** — when the L2.5 orchestrator heals an
   attenuated or saturated mixer, the correction has to survive
   reboot. `alsactl store -f` persists the live state to
   `/var/lib/alsa/asound.state`, which is root-owned. The daemon runs
   unprivileged, so L2.5 calls `systemctl start --no-block
   sovyx-audio-mixer-persist.service` instead — the packaged unit
   runs `alsactl store` as root with the same capability-bounded
   sandbox as the runtime_pm oneshot.

**The daemon writes to neither `/sys/bus/pci/**/power/control` nor
`/var/lib/alsa/asound.state` at runtime**. All elevated writes happen:

1. At boot via `sovyx-audio-runtime-pm.service` (systemd oneshot).
2. On udev hotplug / resume via `60-sovyx-audio-power.rules`.
3. On-demand after an L2.5 heal via `sovyx-audio-mixer-persist.service`
   (systemd oneshot, not enabled at boot — triggered via
   `systemctl start`).

## Install paths

| File | Destination |
|---|---|
| `sovyx-audio-runtime-pm.service` | `/etc/systemd/system/sovyx-audio-runtime-pm.service` (or `/usr/lib/systemd/system/` for vendor installs) |
| `audio-runtime-pm-setup` | `/usr/libexec/sovyx/audio-runtime-pm-setup` (must be executable, `0755 root:root`) |
| `sovyx-audio-mixer-persist.service` | `/etc/systemd/system/sovyx-audio-mixer-persist.service` (or `/usr/lib/systemd/system/`) — NOT enabled at boot; triggered on-demand by the daemon |
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
      /etc/systemd/system/sovyx-audio-mixer-persist.service \
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
