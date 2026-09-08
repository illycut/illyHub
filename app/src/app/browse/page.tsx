"use client";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { BrowseDetail } from "@/components/BrowseDetail";
import { parseRefKey } from "@/lib/hub/library";
import { TextSkeleton } from "@/components/Skeleton";

/**
 * Detail route. Static export cannot prerender dynamic segments, so the content ref travels in
 * the query string: `/browse?ref=tidal:album:123`.
 */
function BrowsePage() {
  const params = useSearchParams();
  const ref = parseRefKey(params.get("ref"));
  if (!ref) {
    return (
      <main className="screen-margin pt-safe">
        <p className="py-6 text-body text-secondary">Nothing to show. Pick an album or playlist from the home screen.</p>
      </main>
    );
  }
  return <BrowseDetail contentRef={ref} />;
}

export default function Page() {
  return (
    <Suspense fallback={<main className="screen-margin pt-6"><TextSkeleton lines={4} /></main>}>
      <BrowsePage />
    </Suspense>
  );
}
