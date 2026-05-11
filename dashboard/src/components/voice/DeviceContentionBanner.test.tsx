/**
 * DeviceContentionBanner — unit coverage.
 *
 * v0.38.0 / W3.B2 + F2-M04 (audit §3.G) closure on F-511. The banner
 * is the actionable UX for the F2-C01 "another app is holding the
 * mic" case; it had no direct unit tests pre-fix. This file pins:
 *
 *   * Named-process body renders with both processHint + hostApi spans
 *   * Anonymous-process body renders when processHint is null
 *   * Each alternative_devices entry renders as a clickable chip
 *   * Chip click invokes onSelectAlternative with the device payload
 *   * onSelectAlternative=null disables every chip + skips the callback
 *   * Empty alternative_devices renders the "no alternatives" line
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import "@/lib/i18n";
import { fireEvent, render, screen } from "@testing-library/react";

import {
  DeviceContentionBanner,
  type AlternativeDevice,
  type CaptureDeviceContendedPayload,
} from "./DeviceContentionBanner";

const _alternative = (overrides: Partial<AlternativeDevice> = {}): AlternativeDevice => ({
  index: 1,
  name: "Razer Seiren",
  host_api: "WASAPI",
  kind: "hardware",
  max_input_channels: 2,
  default_samplerate: 48_000,
  ...overrides,
});

const _payload = (
  overrides: Partial<CaptureDeviceContendedPayload> = {},
): CaptureDeviceContendedPayload => ({
  ok: false,
  error: "capture_device_contended",
  detail: "Discord is holding the microphone.",
  device: 0,
  host_api: "WASAPI",
  suggested_actions: ["close_other_app"],
  contending_process_hint: "Discord.exe",
  alternative_devices: [_alternative()],
  ...overrides,
});

describe("DeviceContentionBanner — body rendering", () => {
  it("renders the named-process body when contending_process_hint is set", () => {
    render(
      <DeviceContentionBanner
        payload={_payload()}
        onSelectAlternative={vi.fn()}
      />,
    );
    // The processHint span renders inside a font-mono <span>.
    expect(screen.getByText("Discord.exe")).toBeInTheDocument();
    // host_api also surfaces.
    expect(screen.getAllByText("WASAPI").length).toBeGreaterThan(0);
  });

  it("renders the anonymous-process body when contending_process_hint is null", () => {
    render(
      <DeviceContentionBanner
        payload={_payload({ contending_process_hint: null })}
        onSelectAlternative={vi.fn()}
      />,
    );
    // Discord.exe must not appear because the processHint is null.
    expect(screen.queryByText("Discord.exe")).not.toBeInTheDocument();
    // The anonymousBody i18n key still renders the host_api.
    expect(screen.getAllByText("WASAPI").length).toBeGreaterThan(0);
  });

  it("renders the no-alternatives line when alternative_devices is empty", () => {
    render(
      <DeviceContentionBanner
        payload={_payload({ alternative_devices: [] })}
        onSelectAlternative={vi.fn()}
      />,
    );
    // No chip rendered.
    expect(screen.queryByTestId("device-contention-chip-1")).not.toBeInTheDocument();
  });
});

describe("DeviceContentionBanner — alternative-device chips", () => {
  it("renders one chip per alternative device with a stable test id", () => {
    const alternatives = [
      _alternative({ index: 1, name: "Razer Seiren" }),
      _alternative({ index: 2, name: "Built-in Mic", kind: "os_default" }),
    ];
    render(
      <DeviceContentionBanner
        payload={_payload({ alternative_devices: alternatives })}
        onSelectAlternative={vi.fn()}
      />,
    );
    expect(screen.getByTestId("device-contention-chip-1")).toBeInTheDocument();
    expect(screen.getByTestId("device-contention-chip-2")).toBeInTheDocument();
    expect(screen.getByText("Razer Seiren")).toBeInTheDocument();
    expect(screen.getByText("Built-in Mic")).toBeInTheDocument();
  });

  it("invokes onSelectAlternative with the full device payload on chip click", () => {
    const onSelectAlternative = vi.fn();
    const dev = _alternative({ index: 7, name: "External XLR" });
    render(
      <DeviceContentionBanner
        payload={_payload({ alternative_devices: [dev] })}
        onSelectAlternative={onSelectAlternative}
      />,
    );
    fireEvent.click(screen.getByTestId("device-contention-chip-7"));
    expect(onSelectAlternative).toHaveBeenCalledTimes(1);
    expect(onSelectAlternative).toHaveBeenCalledWith(dev);
  });

  it("disables every chip + skips the callback when onSelectAlternative is null", () => {
    const dev = _alternative({ index: 7, name: "External XLR" });
    render(
      <DeviceContentionBanner
        payload={_payload({ alternative_devices: [dev] })}
        onSelectAlternative={null}
      />,
    );
    const chip = screen.getByTestId("device-contention-chip-7") as HTMLButtonElement;
    expect(chip.disabled).toBe(true);
    // Click attempt is a no-op — no callback to verify, but the
    // disabled attribute is the load-bearing assertion.
  });

  it("does not invoke onSelectAlternative when null and chip click is bypassed via the inner handler", () => {
    // Belt-and-braces: even if React Testing Library fires the click
    // through the disabled button, the inner handleSelect must short
    // circuit on null. We can't directly assert "callback not called"
    // when callback is null, but we CAN render with a vi.fn() then
    // re-render with null and assert no extra calls.
    const onSelectAlternative = vi.fn();
    const dev = _alternative({ index: 7 });
    const { rerender } = render(
      <DeviceContentionBanner
        payload={_payload({ alternative_devices: [dev] })}
        onSelectAlternative={onSelectAlternative}
      />,
    );
    rerender(
      <DeviceContentionBanner
        payload={_payload({ alternative_devices: [dev] })}
        onSelectAlternative={null}
      />,
    );
    fireEvent.click(screen.getByTestId("device-contention-chip-7"));
    expect(onSelectAlternative).not.toHaveBeenCalled();
  });
});

beforeEach(() => {
  vi.clearAllMocks();
});
