/**
 * NotFound page tests — POLISH-16.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@/test/test-utils";
import NotFoundPage from "./not-found";

describe("NotFoundPage", () => {
  it("renders 404 heading", () => {
    render(<NotFoundPage />);
    expect(screen.getByText("404")).toBeInTheDocument();
  });

  it("has a link back to home", () => {
    render(<NotFoundPage />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/");
  });
});
