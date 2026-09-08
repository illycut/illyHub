import type { Metadata, Viewport } from "next";
import "@/styles/globals.css";
import { HubProvider } from "@/components/HubProvider";
import { Toasts } from "@/components/Toasts";
import { HubBanner } from "@/components/Banner";
import { ServiceWorker } from "@/components/ServiceWorker";

const THEME = "#101014"; // --bg-base; Next metadata cannot read CSS variables
const BUILD_ID = process.env.NEXT_PUBLIC_BUILD_ID ?? new Date().toISOString().slice(0, 16);

export const metadata: Metadata = {
  title: "illyHub",
  description: "Multi-room audio control for HEOS and Sonos",
  manifest: "/manifest.webmanifest",
  appleWebApp: { capable: true, statusBarStyle: "black-translucent", title: "illyHub" },
  icons: { icon: "/icons/icon.svg", apple: "/icons/apple-touch-icon.png" },
};

export const viewport: Viewport = {
  themeColor: THEME,
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  colorScheme: "dark",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preload" href="/fonts/GeneralSans-500.woff2" as="font" type="font/woff2" crossOrigin="anonymous" />
        <link rel="preload" href="/fonts/GeneralSans-600.woff2" as="font" type="font/woff2" crossOrigin="anonymous" />
        <link rel="preload" href="/fonts/GeneralSans-700.woff2" as="font" type="font/woff2" crossOrigin="anonymous" />
      </head>
      <body>
        <HubProvider>
          {/* Banner sits above every surface, Now Playing included (UX U4). */}
          <div className="fixed inset-x-0 top-0 z-50 pt-safe">
            <HubBanner />
          </div>
          {children}
          <Toasts />
        </HubProvider>
        <ServiceWorker version={BUILD_ID} />
      </body>
    </html>
  );
}
