/**
 * Route configuration — exports `router` (non-component), which triggers
 * react-refresh/only-export-components. This is intentional: this file is
 * a route config, not a component module. HMR works via the lazy() imports.
 */
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
  PluginsSkeleton,
} from "@/components/skeletons";

const OverviewPage = lazy(() => import("@/pages/overview"));
const ConversationsPage = lazy(() => import("@/pages/conversations"));
const BrainPage = lazy(() => import("@/pages/brain"));
const EmotionsPage = lazy(() => import("@/pages/emotions"));
const ProductivityPage = lazy(() => import("@/pages/productivity"));
const LogsPage = lazy(() => import("@/pages/logs"));
const SettingsPage = lazy(() => import("@/pages/settings"));
const AboutPage = lazy(() => import("@/pages/about"));
const VoicePage = lazy(() => import("@/pages/voice"));
const VoiceHealthPage = lazy(() => import("@/pages/voice-health"));
const VoicePlatformDiagnosticsPage = lazy(
  () => import("@/pages/voice-platform-diagnostics"),
);
// Mission H4 §4.8 ADR-D8 + v0.49.25 — dedicated engine resources route
// hosting the H4 cohort widgets + deep-link sub-routes for the persisted
// forensic snapshot files referenced by the per-cohort action chips.
const EngineResourcesPage = lazy(() => import("@/pages/engine-resources"));
const EngineResourcesHeapSnapshotPage = lazy(
  () => import("@/pages/engine-resources-heap-snapshot"),
);
const EngineResourcesThreadSnapshotPage = lazy(
  () => import("@/pages/engine-resources-thread-snapshot"),
);
const ChatPage = lazy(() => import("@/pages/chat"));
const PluginsPage = lazy(() => import("@/pages/plugins"));
const NotFoundPage = lazy(() => import("@/pages/not-found"));
const OnboardingPage = lazy(() => import("@/pages/onboarding"));

function PageWrapper({
  name,
  children,
  fallback,
}: {
  name: string;
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
    <ErrorBoundary name={`route.${name}`}>
      <Suspense fallback={fallback ?? defaultFallback}>
        {children}
      </Suspense>
    </ErrorBoundary>
  );
}

export const router = createBrowserRouter([
  {
    path: "onboarding",
    element: (
      <ErrorBoundary name="route.onboarding">
        <Suspense fallback={null}>
          <OnboardingPage />
        </Suspense>
      </ErrorBoundary>
    ),
  },
  {
    element: <AppLayout />,
    children: [
      {
        index: true,
        element: (
          <PageWrapper name="overview" fallback={<OverviewSkeleton />}>
            <OverviewPage />
          </PageWrapper>
        ),
      },
      {
        path: "chat",
        element: (
          <PageWrapper name="chat">
            <ChatPage />
          </PageWrapper>
        ),
      },
      {
        path: "conversations",
        element: (
          <PageWrapper name="conversations" fallback={<ConversationsSkeleton />}>
            <ConversationsPage />
          </PageWrapper>
        ),
      },
      {
        path: "brain",
        element: (
          <PageWrapper name="brain" fallback={<BrainSkeleton />}>
            <BrainPage />
          </PageWrapper>
        ),
      },
      {
        path: "emotions",
        element: (
          <PageWrapper name="emotions">
            <EmotionsPage />
          </PageWrapper>
        ),
      },
      {
        path: "productivity",
        element: (
          <PageWrapper name="productivity">
            <ProductivityPage />
          </PageWrapper>
        ),
      },
      {
        path: "logs",
        element: (
          <PageWrapper name="logs" fallback={<LogsSkeleton />}>
            <LogsPage />
          </PageWrapper>
        ),
      },
      {
        path: "settings",
        element: (
          <PageWrapper name="settings" fallback={<SettingsSkeleton />}>
            <SettingsPage />
          </PageWrapper>
        ),
      },
      {
        path: "plugins",
        element: (
          <PageWrapper name="plugins" fallback={<PluginsSkeleton />}>
            <PluginsPage />
          </PageWrapper>
        ),
      },
      {
        path: "about",
        element: (
          <PageWrapper name="about">
            <AboutPage />
          </PageWrapper>
        ),
      },
      {
        path: "voice",
        element: (
          <PageWrapper name="voice">
            <VoicePage />
          </PageWrapper>
        ),
      },
      {
        path: "voice/health",
        element: (
          <PageWrapper name="voice-health">
            <VoiceHealthPage />
          </PageWrapper>
        ),
      },
      {
        path: "voice/platform-diagnostics",
        element: (
          <PageWrapper name="voice-platform-diagnostics">
            <VoicePlatformDiagnosticsPage />
          </PageWrapper>
        ),
      },
      {
        path: "engine/resources",
        element: (
          <PageWrapper name="engine-resources">
            <EngineResourcesPage />
          </PageWrapper>
        ),
      },
      {
        path: "engine/resources/heap-snapshot/:ts",
        element: (
          <PageWrapper name="engine-resources-heap-snapshot">
            <EngineResourcesHeapSnapshotPage />
          </PageWrapper>
        ),
      },
      {
        path: "engine/resources/thread-snapshot/:ts",
        element: (
          <PageWrapper name="engine-resources-thread-snapshot">
            <EngineResourcesThreadSnapshotPage />
          </PageWrapper>
        ),
      },
      {
        path: "*",
        element: (
          <PageWrapper name="not-found">
            <NotFoundPage />
          </PageWrapper>
        ),
      },
    ],
  },
]);
