/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from "react";
import { createBrowserRouter } from "react-router";
import { AppLayout } from "@/components/layout/app-layout";
import { Skeleton } from "@/components/ui/skeleton";

const OverviewPage = lazy(() => import("@/pages/overview"));
const ConversationsPage = lazy(() => import("@/pages/conversations"));
const BrainPage = lazy(() => import("@/pages/brain"));
const LogsPage = lazy(() => import("@/pages/logs"));
const SettingsPage = lazy(() => import("@/pages/settings"));
const NotFoundPage = lazy(() => import("@/pages/not-found"));

function PageSuspense({ children }: { children: React.ReactNode }) {
  return (
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
  );
}

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      {
        index: true,
        element: (
          <PageSuspense>
            <OverviewPage />
          </PageSuspense>
        ),
      },
      {
        path: "conversations",
        element: (
          <PageSuspense>
            <ConversationsPage />
          </PageSuspense>
        ),
      },
      {
        path: "brain",
        element: (
          <PageSuspense>
            <BrainPage />
          </PageSuspense>
        ),
      },
      {
        path: "logs",
        element: (
          <PageSuspense>
            <LogsPage />
          </PageSuspense>
        ),
      },
      {
        path: "settings",
        element: (
          <PageSuspense>
            <SettingsPage />
          </PageSuspense>
        ),
      },
      {
        path: "*",
        element: (
          <PageSuspense>
            <NotFoundPage />
          </PageSuspense>
        ),
      },
    ],
  },
]);
