/**
 * ErrorBoundary — catches React render errors with retry.
 *
 * Uses --svx-* tokens. No shadcn Card wrapper (matches other pages).
 * Shows error message in dev mode, generic message in prod.
 * Uses i18n.t() directly (class components can't use hooks).
 *
 * On `componentDidCatch` posts an error report to
 * ``POST /api/telemetry/frontend-error`` so unhandled SPA crashes land
 * in the same structlog stream as backend errors. Best-effort: the POST
 * never throws, so a server-side failure cannot cascade into another
 * ErrorBoundary render.
 *
 * Ref: DASH-36, Architecture §7
 */

import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangleIcon, RefreshCwIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import i18n from "@/lib/i18n";
import { apiFetch } from "@/lib/api";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
  /** Skip telemetry POST (used by tests). */
  disableTelemetry?: boolean;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

function reportFrontendError(error: Error, info: ErrorInfo): void {
  const payload = {
    name: error.name,
    message: error.message,
    stack: error.stack?.slice(0, 4_000),
    component_stack: info.componentStack?.slice(0, 4_000),
    url: typeof window !== "undefined" ? window.location.href : undefined,
    user_agent:
      typeof navigator !== "undefined" ? navigator.userAgent : undefined,
  };
  // Fire-and-forget — swallow every error, including apiFetch failures.
  apiFetch("/api/telemetry/frontend-error", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).catch(() => {
    /* telemetry is best-effort */
  });
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    if (this.props.disableTelemetry) return;
    try {
      reportFrontendError(error, info);
    } catch {
      /* never let telemetry break the boundary */
    }
  }

  handleRetry = () => {
    this.setState({ hasError: false, error: null });
  };

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;

      return (
        <div className="mx-auto mt-12 flex max-w-md flex-col items-center gap-4 rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] px-6 py-10 text-center">
          <AlertTriangleIcon className="size-10 text-[var(--svx-color-error)] opacity-60" />
          <div>
            <h2 className="text-lg font-semibold text-[var(--svx-color-text-primary)]">
              {i18n.t("common:errors.generic")}
            </h2>
            <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
              {this.state.error?.message ?? i18n.t("common:errors.unexpected")}
            </p>
          </div>
          <Button onClick={this.handleRetry} variant="secondary" className="gap-2">
            <RefreshCwIcon className="size-4" />
            {i18n.t("common:errors.tryAgain")}
          </Button>
        </div>
      );
    }

    return this.props.children;
  }
}
