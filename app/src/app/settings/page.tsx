"use client";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { SettingsScreen } from "@/components/SettingsScreen";
import { TextSkeleton } from "@/components/Skeleton";
import type { Service } from "@/lib/hub/library";

function SettingsPage() {
  const params = useSearchParams();
  const link = params.get("link");
  const initialLink: Service | null = link === "tidal" || link === "ytmusic" || link === "pandora" ? link : null;
  return <SettingsScreen initialLink={initialLink} />;
}

export default function Page() {
  return (
    <Suspense fallback={<main className="screen-margin pt-6"><TextSkeleton lines={4} /></main>}>
      <SettingsPage />
    </Suspense>
  );
}
