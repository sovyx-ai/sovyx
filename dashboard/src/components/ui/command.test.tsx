import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import {
  Command,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
} from "./command";

// cmdk calls scrollIntoView on selected items — jsdom lacks it.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

describe("Command palette", () => {
  it("renders the command root and list", () => {
    const { container } = render(
      <Command>
        <CommandInput placeholder="Type a command..." />
        <CommandList>
          <CommandGroup heading="Actions">
            <CommandItem>New file</CommandItem>
            <CommandItem>Open file</CommandItem>
          </CommandGroup>
        </CommandList>
      </Command>,
    );
    expect(container.querySelector("[data-slot='command']")).not.toBeNull();
    expect(screen.getByText("New file")).toBeInTheDocument();
    expect(screen.getByText("Open file")).toBeInTheDocument();
  });

  it("filters items based on the input text (cmdk default behavior)", () => {
    render(
      <Command>
        <CommandInput placeholder="filter" />
        <CommandList>
          <CommandEmpty>Nothing found</CommandEmpty>
          <CommandGroup>
            <CommandItem>apple</CommandItem>
            <CommandItem>banana</CommandItem>
          </CommandGroup>
        </CommandList>
      </Command>,
    );
    const input = screen.getByPlaceholderText("filter");
    fireEvent.change(input, { target: { value: "apl" } });
    // cmdk is fuzzy — "apl" should match apple and hide banana
    expect(screen.getByText("apple")).toBeInTheDocument();
  });
});
