/**
 * Tests for :class:`AudioLevelMeter`.
 */
import { render, screen } from "@testing-library/react";
import { AudioLevelMeter, __testables } from "./AudioLevelMeter";

const { dbToFraction, colorForDb, FLOOR_DB, CEIL_DB, VAD_DB, WARN_DB } =
  __testables;

describe("dbToFraction", () => {
  it("returns 0 at the floor", () => {
    expect(dbToFraction(FLOOR_DB)).toBe(0);
  });

  it("returns 1 at full-scale", () => {
    expect(dbToFraction(CEIL_DB)).toBe(1);
  });

  it("clamps values below the floor", () => {
    expect(dbToFraction(-200)).toBe(0);
  });

  it("clamps values above the ceiling", () => {
    expect(dbToFraction(6)).toBe(1);
  });

  it("is roughly linear between floor and ceiling", () => {
    expect(dbToFraction(-60)).toBeCloseTo(0.5, 2);
  });
});

describe("colorForDb", () => {
  it("returns green below the VAD threshold", () => {
    expect(colorForDb(VAD_DB - 1)).toBe("#22c55e");
  });

  it("returns yellow between VAD and WARN", () => {
    expect(colorForDb(VAD_DB + 1)).toBe("#eab308");
  });

  it("returns red above the WARN threshold", () => {
    expect(colorForDb(WARN_DB + 0.5)).toBe("#ef4444");
  });
});

describe("AudioLevelMeter component", () => {
  it("renders a canvas and meter role", () => {
    render(<AudioLevelMeter level={null} />);
    const meter = screen.getByRole("meter");
    expect(meter).toBeInTheDocument();
    expect(meter.getAttribute("aria-valuetext")).toBe("no signal");
    expect(screen.getByTestId("audio-level-meter-canvas")).toBeInTheDocument();
  });

  it("reports dB + flags through aria-valuetext", () => {
    render(
      <AudioLevelMeter
        level={{
          v: 1,
          t: "level",
          rms_db: -24.5,
          peak_db: -18,
          hold_db: -18,
          clipping: true,
          vad_trigger: true,
        }}
      />,
    );
    const meter = screen.getByRole("meter");
    expect(meter.getAttribute("aria-valuetext")).toContain("-24.5 dBFS");
    expect(meter.getAttribute("aria-valuetext")).toContain("clipping");
    expect(meter.getAttribute("aria-valuetext")).toContain("voice detected");
  });

  it("supports custom aria label", () => {
    render(
      <AudioLevelMeter level={null} label="Microphone input" />,
    );
    expect(
      screen.getByRole("meter", { name: "Microphone input" }),
    ).toBeInTheDocument();
  });
});
