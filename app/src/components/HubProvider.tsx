"use client";
import { useEffect } from "react";
import { HubSocket } from "@/lib/hub/socket";
import { hubWsUrl } from "@/lib/hub/config";
import { useHub } from "@/lib/hub/store";

/** Mounts the WebSocket once and wires it to the store, including the hub clock offset. */
export function HubProvider({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    const store = useHub.getState();
    const socket = new HubSocket(hubWsUrl(), {
      onMessage: (m) => useHub.getState().onMessage(m),
      onPhase: (phase, at) => useHub.getState().setPhase(phase, at),
      onClockOffset: (ms) => useHub.getState().setClockOffset(ms),
    });
    store.setResync(() => socket.resync());
    socket.start();
    return () => socket.stop();
  }, []);
  return <>{children}</>;
}
