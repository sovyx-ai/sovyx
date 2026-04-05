import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EmptyState } from "./empty-state";

describe("EmptyState", () => {
  it("renders title and icon", () => {
    render(<EmptyState icon={<span data-testid="icon">🔮</span>} title="No data" />);
    expect(screen.getByText("No data")).toBeInTheDocument();
    expect(screen.getByTestId("icon")).toBeInTheDocument();
  });

  it("renders description when provided", () => {
    render(
      <EmptyState
        icon={<span>🔮</span>}
        title="Empty"
        description="Nothing here yet"
      />,
    );
    expect(screen.getByText("Nothing here yet")).toBeInTheDocument();
  });

  it("renders action button and calls onClick", async () => {
    const onClick = vi.fn();
    render(
      <EmptyState
        icon={<span>🔮</span>}
        title="Empty"
        action={{ label: "Retry", onClick }}
      />,
    );
    const btn = screen.getByRole("button", { name: "Retry" });
    await userEvent.click(btn);
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("does not render action when not provided", () => {
    render(<EmptyState icon={<span>🔮</span>} title="Empty" />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
