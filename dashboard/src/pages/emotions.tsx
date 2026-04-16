import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { Loader2Icon, HeartIcon } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  CurrentMood,
  MoodTimeline,
  PADScatter,
  EmotionalTriggers,
  MoodDistribution,
} from "@/components/emotions";

interface MoodState {
  valence: number;
  arousal: number;
  dominance: number;
  label: string;
  description: string;
  quadrant: string;
  episode_count: number;
}

interface TimelinePoint {
  timestamp: string;
  valence: number;
  arousal: number;
  dominance: number;
  episode_id: string;
  summary: string;
}

interface Trigger {
  concept: string;
  category: string;
  valence: number;
  arousal: number;
  label: string;
  access_count: number;
}

interface DistributionData {
  positive_active: number;
  positive_passive: number;
  negative_active: number;
  negative_passive: number;
  neutral: number;
}

export default function EmotionsPage() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [mood, setMood] = useState<MoodState | null>(null);
  const [timeline, setTimeline] = useState<TimelinePoint[]>([]);
  const [timelinePeriod, setTimelinePeriod] = useState("7d");
  const [triggers, setTriggers] = useState<Trigger[]>([]);
  const [distribution, setDistribution] = useState<DistributionData | null>(null);
  const [distTotal, setDistTotal] = useState(0);
  const [distPeriod, setDistPeriod] = useState("30d");

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [moodRes, trigRes] = await Promise.all([
        api.get<MoodState>("/api/emotions/current"),
        api.get<{ triggers: Trigger[] }>("/api/emotions/triggers?limit=10"),
      ]);
      setMood(moodRes);
      setTriggers(trigRes.triggers);
    } catch {
      // Graceful degradation
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchTimeline = useCallback(async (period: string) => {
    try {
      const res = await api.get<{ points: TimelinePoint[] }>(
        `/api/emotions/timeline?period=${period}`,
      );
      setTimeline(res.points);
    } catch {
      setTimeline([]);
    }
  }, []);

  const fetchDistribution = useCallback(async (period: string) => {
    try {
      const res = await api.get<{
        distribution: DistributionData;
        total: number;
      }>(`/api/emotions/distribution?period=${period}`);
      setDistribution(res.distribution);
      setDistTotal(res.total);
    } catch {
      setDistribution(null);
      setDistTotal(0);
    }
  }, []);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

  useEffect(() => {
    void fetchTimeline(timelinePeriod);
  }, [fetchTimeline, timelinePeriod]);

  useEffect(() => {
    void fetchDistribution(distPeriod);
  }, [fetchDistribution, distPeriod]);

  if (loading && !mood) {
    return (
      <div className="flex min-h-[300px] items-center justify-center gap-2 text-[var(--svx-color-text-secondary)]">
        <Loader2Icon className="size-5 animate-spin" />
        <span>Loading emotional data...</span>
      </div>
    );
  }

  if (mood && mood.episode_count === 0) {
    return (
      <div className="flex min-h-[400px] flex-col items-center justify-center gap-4 text-center">
        <HeartIcon className="size-12 text-[var(--svx-color-text-disabled)]" />
        <div>
          <h2 className="text-lg font-semibold text-[var(--svx-color-text-primary)]">
            Your Emotional Landscape
          </h2>
          <p className="mt-1 max-w-md text-sm text-[var(--svx-color-text-secondary)]">
            Start a conversation to see how your interactions shape the
            emotional profile of your mind. Every exchange adds a data point.
          </p>
        </div>
        <Button variant="outline" onClick={() => navigate("/chat")}>
          Go to Chat
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold text-[var(--svx-color-text-primary)]">
          Emotions
        </h1>
        <p className="text-sm text-[var(--svx-color-text-secondary)]">
          PAD 3D emotional model — Pleasure, Arousal, Dominance
        </p>
      </div>

      {mood && (
        <CurrentMood
          valence={mood.valence}
          arousal={mood.arousal}
          dominance={mood.dominance}
          label={mood.label}
          description={mood.description}
          quadrant={mood.quadrant}
          episodeCount={mood.episode_count}
        />
      )}

      <MoodTimeline
        points={timeline}
        period={timelinePeriod}
        onPeriodChange={setTimelinePeriod}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <PADScatter points={timeline} />
        {distribution && (
          <MoodDistribution
            distribution={distribution}
            total={distTotal}
            period={distPeriod}
            onPeriodChange={setDistPeriod}
          />
        )}
      </div>

      <EmotionalTriggers triggers={triggers} />
    </div>
  );
}
