/**
 * ErrorBoundary — catches React render errors with retry.
 *
 * Uses --svx-* tokens. No shadcn Card wrapper (matches other pages).
 * Shows error message in dev mode, generic message in prod.
 *
 * Ref: DASH-36, Architecture §7
 */

import { Component, type ReactNode } from "react";
import { AlertTriangleIcon, RefreshCwIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
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
              Something went wrong
            </h2>
            <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
              {this.state.error?.message ?? "An unexpected error occurred."}
            </p>
          </div>
          <Button onClick={this.handleRetry} variant="secondary" className="gap-2">
            <RefreshCwIcon className="size-4" />
            Try Again
          </Button>
        </div>
      );
    }

    return this.props.children;
  }
}
