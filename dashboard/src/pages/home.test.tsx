import { describe, it, expect } from "vitest";
import { render, screen } from "@/test/test-utils";
import HomePage from "./home";

describe("HomePage", () => {
  it("renders page title", () => {
    render(<HomePage />);
    expect(screen.getByText("Home Integration")).toBeInTheDocument();
  });

  it("renders description", () => {
    render(<HomePage />);
    expect(screen.getByText(/Control your smart home/)).toBeInTheDocument();
  });

  it("renders feature list with expected items", () => {
    render(<HomePage />);
    expect(screen.getByText("Home Assistant entity list")).toBeInTheDocument();
    expect(screen.getByText("Quick actions")).toBeInTheDocument();
    expect(screen.getByText("Automation status")).toBeInTheDocument();
    expect(screen.getByText("Camera snapshots")).toBeInTheDocument();
  });

  it("shows v1.0 version badge", () => {
    render(<HomePage />);
    expect(screen.getByText("Coming in v1.0")).toBeInTheDocument();
  });
});
