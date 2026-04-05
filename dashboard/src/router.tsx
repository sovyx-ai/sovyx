/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from "react";
import { createBrowserRouter } from "react-router";
import { AppLayout } from "@/components/layout/app-layout";
import { ErrorBoundary } from "@/components/error-boundary";
import { Skeleton } from "@/components/ui/skeleton";
import {
  OverviewSkeleton,
  ConversationsSkeleton,
  BrainSkeleton,
  LogsSkeleton,
  SettingsSkeleton,
} from "@/components/skeletons";

const OverviewPage = lazy(() => import("@/pages/overview"));
const ConversationsPage = lazy(() => import("@/pages/conversations"));
const BrainPage = lazy(() => import("@/pages/brain"));
const LogsPage = lazy(() => import("@/pages/logs"));
const SettingsPage = lazy(() => import("@/pages/settings"));
const NotFoundPage = lazy(() => import("@/pages/not-found"));

function PageWrapper({
  children,
  fallback,
}: {
  children: React.ReactNode;
  fallback?: React.ReactNode;
}) {
  const defaultFallback = (
    <div className="space-y-4">
      <Skeleton className="h-8 w-48" />
      <Skeleton className="h-48 w-full" />
    </div>
  );

  return (
    <ErrorBoundary>
      <Suspense fallback={fallback ?? defaultFallback}>
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
          <PageWrapper fallback={<OverviewSkeleton />}>
            <OverviewPage />
          </PageWrapper>
        ),
      },
      {
        path: "conversations",
        element: (
          <PageWrapper fallback={<ConversationsSkeleton />}>
            <ConversationsPage />
          </PageWrapper>
        ),
      },
      {
        path: "brain",
        element: (
          <PageWrapper fallback={<BrainSkeleton />}>
            <BrainPage />
          </PageWrapper>
        ),
      },
      {
        path: "logs",
        element: (
          <PageWrapper fallback={<LogsSkeleton />}>
            <LogsPage />
          </PageWrapper>
        ),
      },
      {
        path: "settings",
        element: (
          <PageWrapper fallback={<SettingsSkeleton />}>
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
