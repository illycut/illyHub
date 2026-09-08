/** Loading states (design system §8): neutral bg-raised blocks, no shimmer, art holds the square. */
export function Skeleton({ className = "" }: { className?: string }) {
  return <div aria-hidden="true" data-skeleton className={`rounded-control bg-raised ${className}`} />;
}

export function ArtSkeleton({ className = "" }: { className?: string }) {
  return <div aria-hidden="true" data-skeleton className={`aspect-square w-full rounded-art bg-raised ${className}`} />;
}

/** Title / artist / badge placeholders for Now Playing and the mini-player. */
export function TextSkeleton({ lines = 2, className = "" }: { lines?: number; className?: string }) {
  return (
    <div className={`flex flex-col gap-2 ${className}`} aria-hidden="true" data-skeleton>
      {Array.from({ length: lines }).map((_, i) => (
        <div key={i} className={`h-4 rounded-control bg-raised ${i === 0 ? "w-3/4" : "w-1/2"}`} />
      ))}
    </div>
  );
}

export function EmptyState({ children }: { children: React.ReactNode }) {
  return <p className="py-6 text-body text-secondary">{children}</p>;
}

/**
 * Grid-position card: "Connect Tidal" / "Connect YouTube Music" (design system §8). Tapping
 * starts the hub sign-in flow; a disabled card carries a short note instead.
 */
export function ConnectCard({ service, onPress, note }: { service: string; onPress?: () => void; note?: string }) {
  const cls = "flex aspect-square w-full flex-col items-center justify-center gap-2 rounded-art border border-stroke bg-raised p-3 text-center";
  if (!onPress) {
    // Disabled: no opacity; title text-secondary, reason text-tertiary (UX U5).
    return (
      <div className={cls} data-testid="connect-card" aria-disabled="true">
        <span className="text-body text-secondary">Connect {service}</span>
        {note ? <span className="text-micro text-tertiary">{note}</span> : null}
      </div>
    );
  }
  const body = (
    <>
      <span className="text-body text-primary">Connect {service}</span>
      {note ? <span className="text-micro text-tertiary">{note}</span> : null}
    </>
  );
  return (
    <button type="button" className={`${cls} transition-transform duration-press active:scale-[0.97]`} onClick={onPress} data-testid="connect-card">
      {body}
    </button>
  );
}
