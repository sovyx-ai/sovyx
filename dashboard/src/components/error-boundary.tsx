/**
 * ErrorBoundary — catches React render errors with retry.
 *
 * Uses --svx-* tokens. No shadcn Card wrapper (matches other pages).
 * Shows error message in dev mode, generic message in prod.
 * Uses i18n.t() directly (class components can't use hooks).
 *
 * Two variants:
 *
 *   variant="page"    — full-card fallback, used at route level so a
 *                       crashed page doesn't leak into sibling routes.
 *                       The user gets a labeled "Try again" button that
 *                       resets the boundary state (= remounts children).
 *   variant="section" — compact inline alert, used to isolate a single
 *                       block inside a page (e.g. the Safety section of
 *                       Settings, the message thread in Chat). A crash
 *                       in one section doesn't blank the whole page.
 *
 * On `componentDidCatch` posts an error report to
 * ``POST /api/telemetry/frontend-error`` so unhandled SPA crashes land
 * in the same structlog stream as backend errors. The optional `name`
 * prop is forwarded as ``boundary`` so log aggregators can group by
 * route or section (e.g. ``"route.settings"`` vs ``"section.settings.safety"``).
 * Best-effort: the POST never throws, so a server-side failure cannot
 * cascade into another ErrorBoundary render.
 *
 * Ref: DASH-36, Architecture §7
 */

import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangleIcon, RefreshCwIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import i18n from "@/lib/i18n";
import { apiFetch } from "@/lib/api";

type Variant = "page" | "section";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
  /** Logical boundary name sent with telemetry (route or section). */
  name?: string;
  /** Fallback style. Defaults to `"page"`. */
  variant?: Variant;
  /** Skip telemetry POST (used by tests). */
  disableTelemetry?: boolean;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

function reportFrontendError(
  error: Error,
  info: ErrorInfo,
  boundary: string | undefined,
): void {
  const payload = {
    name: error.name,
    message: error.message,
    stack: error.stack?.slice(0, 4_000),
    component_stack: info.componentStack?.slice(0, 4_000),
    url: typeof window !== "undefined" ? window.location.href : undefined,
    user_agent:
      typeof navigator !== "undefined" ? navigator.userAgent : undefined,
    boundary,
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
      reportFrontendError(error, info, this.props.name);
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

      const variant: Variant = this.props.variant ?? "page";
      const message =
        this.state.error?.message ?? i18n.t("common:errors.unexpected");

      if (variant === "section") {
        return (
          <div
            role="alert"
            data-boundary={this.props.name}
            className="flex flex-col gap-2 rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-error)]/40 bg-[var(--svx-color-error-subtle)] p-4 text-sm sm:flex-row sm:items-center sm:justify-between"
          >
            <div className="flex items-start gap-2">
              <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-[var(--svx-color-error)]" />
              <div>
                <p className="font-medium text-[var(--svx-color-text-primary)]">
                  {i18n.t("common:errors.sectionFailed")}
                </p>
                <p className="mt-0.5 text-xs text-[var(--svx-color-text-secondary)]">
                  {message}
                </p>
              </div>
            </div>
            <Button
              onClick={this.handleRetry}
              variant="ghost"
              size="sm"
              className="gap-1.5 self-start sm:self-center"
            >
              <RefreshCwIcon className="size-3.5" />
              {i18n.t("common:errors.tryAgain")}
            </Button>
          </div>
        );
      }

      return (
        <div
          role="alert"
          data-boundary={this.props.name}
          className="mx-auto mt-12 flex max-w-md flex-col items-center gap-4 rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] px-6 py-10 text-center"
        >
          <AlertTriangleIcon className="size-10 text-[var(--svx-color-error)] opacity-60" />
          <div>
            <h2 className="text-lg font-semibold text-[var(--svx-color-text-primary)]">
              {i18n.t("common:errors.generic")}
            </h2>
            <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
              {message}
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
