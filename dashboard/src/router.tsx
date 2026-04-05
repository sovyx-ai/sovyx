/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from "react";
import { createBrowserRouter } from "react-router";
import { AppLayout } from "@/components/layout/app-layout";
import { ErrorBoundary } from "@/components/error-boundary";
import { Skeleton } from "@/components/ui/skeleton";

const OverviewPage = lazy(() => import("@/pages/overview"));
const ConversationsPage = lazy(() => import("@/pages/conversations"));
const BrainPage = lazy(() => import("@/pages/brain"));
const LogsPage = lazy(() => import("@/pages/logs"));
const SettingsPage = lazy(() => import("@/pages/settings"));
const NotFoundPage = lazy(() => import("@/pages/not-found"));

function PageWrapper({ children }: { children: React.ReactNode }) {
  return (
    <ErrorBoundary>
      <Suspense
        fallback={
          <div className="space-y-4">
            <Skeleton className="h-8 w-48" />
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <Skeleton className="h-32" />
              <Skeleton className="h-32" />
              <Skeleton className="h-32" />
            </div>
            <Skeleton className="h-48 w-full" />
          </div>
        }
      >
        {children}
      </Suspense>
    </ErrorBoundary>
  );
}

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      {
        index: true,
        element: (
          <PageWrapper>
            <OverviewPage />
          </PageWrapper>
        ),
      },
      {
        path: "conversations",
        element: (
          <PageWrapper>
            <ConversationsPage />
          </PageWrapper>
        ),
      },
      {
        path: "brain",
        element: (
          <PageWrapper>
            <BrainPage />
          </PageWrapper>
        ),
      },
      {
        path: "logs",
        element: (
          <PageWrapper>
            <LogsPage />
          </PageWrapper>
        ),
      },
      {
        path: "settings",
        element: (
          <PageWrapper>
            <SettingsPage />
          </PageWrapper>
        ),
      },
      {
        path: "*",
        element: (
          <PageWrapper>
            <NotFoundPage />
          </PageWrapper>
        ),
      },
    ],
  },
]);
