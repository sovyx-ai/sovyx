import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

/** Overview page skeleton — 4 stat cards + health grid + chart + feed */
export function OverviewSkeleton() {
  return (
    <div className="space-y-6">
      <div>
        <Skeleton className="h-8 w-32" />
        <Skeleton className="mt-1 h-4 w-48" />
      </div>
      {/* 4 stat cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Card key={i}>
            <CardHeader className="pb-2">
              <Skeleton className="h-4 w-20" />
            </CardHeader>
            <CardContent>
              <Skeleton className="h-7 w-16" />
              <Skeleton className="mt-2 h-3 w-28" />
            </CardContent>
          </Card>
        ))}
      </div>
      {/* Health grid */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-16 rounded-lg" />
        ))}
      </div>
      {/* Chart */}
      <Skeleton className="h-[200px] rounded-lg" />
    </div>
  );
}

/** Conversations page skeleton — split layout */
export function ConversationsSkeleton() {
  return (
    <div className="flex h-[calc(100vh-6rem)] gap-4">
      {/* List panel */}
      <Card className="w-80 shrink-0">
        <CardHeader className="space-y-3 pb-3">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-8 w-full" />
        </CardHeader>
        <CardContent className="space-y-2 p-0 px-3">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="flex items-center gap-3 py-2">
              <Skeleton className="size-7 rounded-full" />
              <div className="flex-1 space-y-1">
                <Skeleton className="h-3 w-24" />
                <Skeleton className="h-2.5 w-16" />
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
      {/* Detail panel */}
      <Card className="flex-1">
        <CardContent className="flex h-full items-center justify-center">
          <Skeleton className="size-10 rounded-full" />
        </CardContent>
      </Card>
    </div>
  );
}

/** Brain page skeleton — legend + graph */
export function BrainSkeleton() {
  return (
    <div className="space-y-4">
      <div>
        <Skeleton className="h-8 w-36" />
        <Skeleton className="mt-1 h-4 w-48" />
      </div>
      {/* Legend */}
      <div className="flex gap-3">
        {Array.from({ length: 7 }).map((_, i) => (
          <div key={i} className="flex items-center gap-1.5">
            <Skeleton className="size-2.5 rounded-full" />
            <Skeleton className="h-3 w-14" />
          </div>
        ))}
      </div>
      {/* Graph */}
      <Skeleton className="h-[calc(100vh-20rem)] min-h-[400px] rounded-lg" />
    </div>
  );
}

/** Logs page skeleton — filters + rows */
export function LogsSkeleton() {
  return (
    <div className="flex h-[calc(100vh-6rem)] flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <Skeleton className="h-8 w-16" />
          <Skeleton className="mt-1 h-4 w-24" />
        </div>
        <div className="flex gap-1">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-7 w-12" />
          ))}
        </div>
      </div>
      <Skeleton className="h-8 w-full" />
      <Card className="flex-1">
        <CardContent className="space-y-1 p-3">
          {Array.from({ length: 20 }).map((_, i) => (
            <div key={i} className="flex items-center gap-3 py-1">
              <Skeleton className="h-3 w-14" />
              <Skeleton className="h-3 w-10" />
              <Skeleton className="h-3 w-24" />
              <Skeleton className="h-3 flex-1" />
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}

/** Settings page skeleton — 3 cards */
export function SettingsSkeleton() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <Skeleton className="h-8 w-24" />
          <Skeleton className="mt-1 h-4 w-48" />
        </div>
        <Skeleton className="h-9 w-20" />
      </div>
      {Array.from({ length: 3 }).map((_, i) => (
        <Card key={i}>
          <CardHeader>
            <Skeleton className="h-4 w-24" />
            <Skeleton className="h-3 w-48" />
          </CardHeader>
          <CardContent className="space-y-4">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
