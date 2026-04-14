import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@/lib/i18n";
import { PermissionDialog } from "./permission-dialog";
import type { PluginPermission } from "@/types/api";

function perm(
  permission: string,
  risk: PluginPermission["risk"],
  description = "",
): PluginPermission {
  return { permission, risk, description };
}

describe("PermissionDialog", () => {
  it("does not render when closed", () => {
    render(
      <PermissionDialog
        open={false}
        onClose={() => {}}
        pluginName="test-plugin"
        permissions={[]}
      />,
    );
    expect(screen.queryByText(/test-plugin/)).not.toBeInTheDocument();
  });

  it("lists each permission name when open", () => {
    render(
      <PermissionDialog
        open={true}
        onClose={() => {}}
        pluginName="finance-plugin"
        permissions={[
          perm("network:internet", "high", "can call any domain"),
          perm("brain:read", "low", "reads memory"),
        ]}
      />,
    );
    expect(screen.getByText("network:internet")).toBeInTheDocument();
    expect(screen.getByText("brain:read")).toBeInTheDocument();
  });

  it("shows install title in install mode", () => {
    render(
      <PermissionDialog
        open={true}
        onClose={() => {}}
        pluginName="plug"
        permissions={[]}
        mode="install"
      />,
    );
    expect(screen.getByText(/Install plug/)).toBeInTheDocument();
  });

  it("renders a close affordance when open", () => {
    render(
      <PermissionDialog
        open={true}
        onClose={() => {}}
        pluginName="plug"
        permissions={[perm("x", "low")]}
      />,
    );
    // At least one button exists (Close + any extras)
    expect(screen.getAllByRole("button").length).toBeGreaterThan(0);
  });
});
