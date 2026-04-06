/**
 * Page skeleton loaders — shown during React.lazy() Suspense.
 *
 * POLISH-07: Uses --svx-* tokens instead of shadcn Card wrappers.
 *
 * Ref: DASH-41
 */

import { Skeleton } from "@/components/ui/skeleton";

const cardClass = "rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)]";

export function OverviewSkeleton() {
  return (
    <div className="space-y-6">
      <div>
        <Skeleton className="h-8 w-32" />
        <Skeleton className="mt-1 h-4 w-48" />
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className={`${cardClass} p-4`}>
            <Skeleton className="h-4 w-20" />
            <Skeleton className="mt-3 h-7 w-16" />
            <Skeleton className="mt-2 h-3 w-28" />
          </div>
        ))}
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-16 rounded-[var(--svx-radius-lg)]" />
        ))}
      </div>
      <Skeleton className="h-[200px] rounded-[var(--svx-radius-lg)]" />
    </div>
  );
}

export function ConversationsSkeleton() {
  return (
    <div className="flex h-[calc(100vh-6rem)] gap-4">
      <div className={`${cardClass} w-80 shrink-0 p-4`}>
        <Skeleton className="h-4 w-24" />
        <Skeleton className="mt-3 h-8 w-full" />
        <div className="mt-4 space-y-3">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="flex items-center gap-3">
              <Skeleton className="size-7 rounded-full" />
              <div className="flex-1 space-y-1">
                <Skeleton className="h-3 w-24" />
                <Skeleton className="h-2.5 w-16" />
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className={`${cardClass} flex flex-1 items-center justify-center`}>
        <Skeleton className="size-10 rounded-full" />
      </div>
    </div>
  );
}

export function BrainSkeleton() {
  return (
    <div className="space-y-4">
      <div>
        <Skeleton className="h-8 w-36" />
        <Skeleton className="mt-1 h-4 w-48" />
      </div>
      <div className="flex gap-3">
        {Array.from({ length: 7 }).map((_, i) => (
          <div key={i} className="flex items-center gap-1.5">
            <Skeleton className="size-2.5 rounded-full" />
            <Skeleton className="h-3 w-14" />
          </div>
        ))}
      </div>
      <Skeleton className="h-[calc(100vh-20rem)] min-h-[400px] rounded-[var(--svx-radius-lg)]" />
    </div>
  );
}

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
      <div className={`${cardClass} flex-1 p-3 space-y-1`}>
        {Array.from({ length: 20 }).map((_, i) => (
          <div key={i} className="flex items-center gap-3 py-1">
            <Skeleton className="h-3 w-14" />
            <Skeleton className="h-3 w-10" />
            <Skeleton className="h-3 w-24" />
            <Skeleton className="h-3 flex-1" />
          </div>
        ))}
      </div>
    </div>
  );
}

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
        <div key={i} className={`${cardClass} p-4 space-y-4`}>
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-3 w-48" />
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
      ))}
    </div>
  );
}
