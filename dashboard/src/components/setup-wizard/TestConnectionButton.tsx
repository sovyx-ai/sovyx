/**
 * TestConnectionButton -- validates plugin config before saving.
 *
 * Calls POST /api/setup/{name}/test-connection with the current form
 * values. Shows success/failure inline with a message from the plugin.
 */

import { memo, useCallback, useState } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { CheckCircle2Icon, XCircleIcon, LoaderIcon } from "lucide-react";
import type { TestConnectionResult } from "./types";

interface TestConnectionButtonProps {
  pluginName: string;
  config: Record<string, unknown>;
  disabled?: boolean;
}

function TestConnectionButtonImpl({
  pluginName,
  config,
  disabled,
}: TestConnectionButtonProps) {
  const [state, setState] = useState<"idle" | "testing" | "success" | "error">(
    "idle",
  );
  const [message, setMessage] = useState("");

  const handleTest = useCallback(async () => {
    setState("testing");
    setMessage("");
    try {
      const result = await api.post<TestConnectionResult>(
        `/api/setup/${pluginName}/test-connection`,
        { config },
      );
      setState(result.success ? "success" : "error");
      setMessage(result.message);
    } catch {
      setState("error");
      setMessage("Connection test failed -- check your network.");
    }
  }, [pluginName, config]);

  return (
    <div className="space-y-2">
      <Button
        variant="outline"
        size="sm"
        onClick={handleTest}
        disabled={disabled || state === "testing"}
        className="w-full"
      >
        {state === "testing" && (
          <LoaderIcon className="mr-2 size-3.5 animate-spin" />
        )}
        {state === "testing" ? "Testing..." : "Test Connection"}
      </Button>

      {state === "success" && (
        <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-success)]/10 px-3 py-2 text-xs text-[var(--svx-color-success)]">
          <CheckCircle2Icon className="size-3.5 shrink-0" />
          <span>{message}</span>
        </div>
      )}

      {state === "error" && (
        <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-error)]/10 px-3 py-2 text-xs text-[var(--svx-color-error)]">
          <XCircleIcon className="size-3.5 shrink-0" />
          <span>{message}</span>
        </div>
      )}
    </div>
  );
}

export const TestConnectionButton = memo(TestConnectionButtonImpl);
