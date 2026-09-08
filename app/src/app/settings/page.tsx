"use client";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { SettingsScreen } from "@/components/SettingsScreen";
import { TextSkeleton } from "@/components/Skeleton";
import { isHubLinked } from "@/lib/services";

function SettingsPage() {
  const params = useSearchParams();
  const link = params.get("link");
  // Only services the hub links itself start a flow; `?link=pandora` (or garbage) renders Settings plainly.
  const initialLink = isHubLinked(link) ? link : null;
  return <SettingsScreen initialLink={initialLink} />;
}

export default function Page() {
  return (
    <Suspense fallback={<main className="screen-margin pt-6"><TextSkeleton lines={4} /></main>}>
      <SettingsPage />
    </Suspense>
  );
}
