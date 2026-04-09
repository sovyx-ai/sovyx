import { useState, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { DollarSignIcon, BrainIcon, MessageSquareIcon, ActivityIcon, MicIcon, HeartIcon, ListTodoIcon } from "lucide-react";
import { useDashboardStore } from "@/stores/dashboard";
import { StatCard, StatCardSkeleton, HealthGrid, ActivityFeed, MetricChart, CognitiveTimeline } from "@/components/dashboard";
import { formatUptime, formatCost, formatNumber } from "@/lib/format";
import { ComingSoon } from "@/components/coming-soon";
import { WelcomeBanner } from "@/components/dashboard/welcome-banner";
import { MindAliveCard } from "@/components/dashboard/mind-alive-card";
import { ChannelStatusCard } from "@/components/dashboard/channel-status";
import { useOnboardingProgress } from "@/hooks/use-onboarding";

/**
 * Format a stat value for fresh-engine state.
 * Shows contextual text instead of dead "0" when the engine has no data yet.
 */
function freshValue(value: number, formatter: (n: number) => string, freshLabel: string): string {
  return value === 0 ? freshLabel : formatter(value);
}

export default function OverviewPage() {
  const { t } = useTranslation(["overview", "common"]);
  const status = useDashboardStore((s) => s.status);
  const healthChecks = useDashboardStore((s) => s.healthChecks);
  const connected = useDashboardStore((s) => s.connected);
  const recentEvents = useDashboardStore((s) => s.recentEvents);
  const costData = useDashboardStore((s) => s.costData);
  const {
    step1, step2, step3, completedCount, allDone,
    showBanner, showAliveCard, setDismissed,
  } = useOnboardingProgress();

  // ── Transition: WelcomeBanner → MindAliveCard (Cenário A vs B) ──
  // Cenário A: user WITNESSES allDone becoming true → animated transition
  // Cenário B: allDone already true on mount → show MindAliveCard directly, no animation
  const wasAllDoneOnMount = useRef(allDone);
  const [transitioning, setTransitioning] = useState(false);
  const [showExiting, setShowExiting] = useState(false);
  const [animateAlive, setAnimateAlive] = useState(false);

  useEffect(() => {
    // Only trigger transition if allDone CHANGED to true (not on mount)
    if (allDone && !wasAllDoneOnMount.current && !transitioning) {
      // Step 1: Show completed state for 1.5s
      setTransitioning(true);
      const exitTimer = setTimeout(() => {
        setShowExiting(true);
        // Step 2: After exit animation (400ms), show alive card
        const enterTimer = setTimeout(() => {
          setShowExiting(false);
          setTransitioning(false);
          setAnimateAlive(true);
        }, 400);
        return () => clearTimeout(enterTimer);
      }, 1500);
      return () => clearTimeout(exitTimer);
    }
  }, [allDone, transitioning]);

  /** True when engine just started with no activity */
  const isFresh = status
    ? status.messages_today === 0 && status.memory_concepts === 0 && status.llm_calls_today === 0
    : false;

  // Determine what to show in the onboarding slot
  const showBannerNow = (showBanner || transitioning) && !showExiting;
  const showAliveNow = (showAliveCard && !transitioning) || false;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t("title")}</h1>
        <p className="text-sm text-[var(--svx-color-text-secondary)]">
          {allDone
            ? t("subtitle")
            : isFresh
              ? t("subtitleFresh")
              : t("subtitleProgress", { defaultValue: "Almost there — finish setting up your Mind." })}
        </p>
      </div>

      {/* Onboarding slot — WelcomeBanner or MindAliveCard with animated transition */}
      {showBannerNow && connected && (
        <div
          className={showExiting ? "animate-[onboarding-exit_400ms_ease-out_forwards]" : ""}
          style={{ minHeight: transitioning ? "200px" : undefined }}
        >
          <WelcomeBanner
            step1={step1}
            step2={step2}
            step3={step3}
            completedCount={completedCount}
            onDismiss={() => setDismissed(true)}
          />
        </div>
      )}
      {showAliveNow && connected && (
        <div className={animateAlive ? "animate-[onboarding-enter_400ms_ease-out_both]" : ""}>
          <MindAliveCard
            animate={animateAlive}
            onDismiss={() => setDismissed(true)}
          />
        </div>
      )}

      {/* 4 Stat Cards — skeleton while loading */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {!status ? (
          <>
            <StatCardSkeleton />
            <StatCardSkeleton />
            <StatCardSkeleton />
            <StatCardSkeleton />
          </>
        ) : (
          <>
            {/* Engine Status */}
            <StatCard
              title={t("cards.engineStatus")}
              value={connected ? t("common:status.online") : t("common:status.offline")}
              subtitle={
                isFresh && connected
                  ? t("cards.justBooted")
                  : t("cards.uptime", { duration: formatUptime(status.uptime_seconds) })
              }
              status={connected ? "green" : "red"}
              icon={<ActivityIcon className="size-4" />}
            />

            {/* Messages */}
            <StatCard
              title={t("cards.messages")}
              value={freshValue(status.messages_today, formatNumber, t("cards.messagesFresh"))}
              subtitle={
                status.active_conversations > 0
                  ? t("cards.activeCount", { count: formatNumber(status.active_conversations) })
                  : t("cards.messagesHint")
              }
              icon={<MessageSquareIcon className="size-4" />}
            />

            {/* Brain */}
            <StatCard
              title={t("cards.brainConcepts")}
              value={freshValue(status.memory_concepts, formatNumber, t("cards.brainFresh"))}
              subtitle={
                status.memory_concepts > 0
                  ? t("cards.episodeCount", { count: status.memory_episodes })
                  : t("cards.brainHint")
              }
              icon={<BrainIcon className="size-4" />}
            />

            {/* LLM Cost */}
            <StatCard
              title={t("cards.llmCost")}
              value={freshValue(status.llm_cost_today, formatCost, t("cards.costFresh"))}
              subtitle={
                status.llm_calls_today > 0
                  ? t("cards.costSubtitle", { calls: formatNumber(status.llm_calls_today), tokens: formatNumber(status.tokens_today) })
                  : t("cards.costHint")
              }
              icon={<DollarSignIcon className="size-4" />}
            />
          </>
        )}
      </div>

      {/* Health Grid */}
      <HealthGrid checks={healthChecks} />

      {/* Cost Chart */}
      <MetricChart
        title={t("chart.costTitle")}
        data={costData}
        color="var(--chart-1)"
        unit="$"
        label={t("chart.costLabel")}
      />

      {/* Channel Status + Cognitive Timeline + Live Feed */}
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-1">
          <ChannelStatusCard />
        </div>
        <div className="lg:col-span-2">
          <CognitiveTimeline />
        </div>
      </div>

      {/* Live Feed */}
      <ActivityFeed events={recentEvents} />

      {/* v1.0 Placeholders */}
      <div className="grid gap-4 md:grid-cols-3">
        <ComingSoon
          title={t("placeholders.voice")}
          description={t("placeholders.voiceDesc")}
          icon={<MicIcon className="size-10" />}
        />
        <ComingSoon
          title={t("placeholders.emotional")}
          description={t("placeholders.emotionalDesc")}
          icon={<HeartIcon className="size-10" />}
        />
        <ComingSoon
          title={t("placeholders.tasks")}
          description={t("placeholders.tasksDesc")}
          icon={<ListTodoIcon className="size-10" />}
        />
      </div>
    </div>
  );
}
