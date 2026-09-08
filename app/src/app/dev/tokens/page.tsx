import { notFound } from "next/navigation";
import { TokenSheet } from "@/components/TokenSheet";

/** Visual QA page for the design tokens. Gated out of production exports unless NEXT_PUBLIC_DEV_TOKENS=1. */
export default function TokensPage() {
  if (process.env.NODE_ENV === "production" && process.env.NEXT_PUBLIC_DEV_TOKENS !== "1") notFound();
  return <TokenSheet />;
}
