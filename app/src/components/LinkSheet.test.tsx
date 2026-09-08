/** Device-code link sheet shared by Tidal and YouTube Music (Phase 6): one view, parameterised by service. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "vitest-axe";
import { LinkSheet, hostOf } from "./LinkSheet";
import type { AuthStart } from "@/lib/hub/library";

vi.mock("framer-motion", async () => {
  const actual = await vi.importActual<typeof import("framer-motion")>("framer-motion");
  return { ...actual, AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</> };
});

const start = (_service: "tidal" | "ytmusic", url: string): AuthStart => ({ user_code: "WXYZ-1234", verification_url: url, expires_in_s: 300, interval_s: 5 });

describe("LinkSheet", () => {
  it("titles itself per service and shows the code, the host link and the waiting line (YouTube Music); passes axe", async () => {
    const { container } = render(<LinkSheet service="ytmusic" phase={{ kind: "code", start: start("ytmusic", "https://www.google.com/device") }} onClose={() => {}} onRetry={() => {}} />);
    expect(screen.getByTestId("link-sheet")).toHaveTextContent("Connect YouTube Music");
    expect(screen.getByTestId("user-code")).toHaveTextContent("WXYZ-1234");
    expect(screen.getByTestId("user-code").className).toContain("numeric");
    expect(screen.getByTestId("open-verification")).toHaveAttribute("href", "https://www.google.com/device");
    expect(screen.getByTestId("open-verification")).toHaveTextContent("Open google.com/device");
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for YouTube Music…");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("the same component serves Tidal", () => {
    render(<LinkSheet service="tidal" phase={{ kind: "code", start: start("tidal", "https://link.tidal.com/") }} onClose={() => {}} onRetry={() => {}} />);
    expect(screen.getByTestId("link-sheet")).toHaveTextContent("Connect Tidal");
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for Tidal…");
    expect(screen.getByTestId("open-verification")).toHaveTextContent("Open link.tidal.com");
  });

  it("expired offers a new code, error offers a retry, linked offers Done; idle renders nothing", async () => {
    const onRetry = vi.fn();
    const onClose = vi.fn();
    const { rerender } = render(<LinkSheet service="ytmusic" phase={{ kind: "expired" }} onClose={onClose} onRetry={onRetry} />);
    expect(screen.getByTestId("link-expired")).toHaveTextContent("This code expired.");
    await userEvent.click(screen.getByTestId("new-code"));
    expect(onRetry).toHaveBeenCalledTimes(1);
    rerender(<LinkSheet service="ytmusic" phase={{ kind: "error", message: "Google said no." }} onClose={onClose} onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Google said no.");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledTimes(2);
    rerender(<LinkSheet service="ytmusic" phase={{ kind: "linked", name: "james@gmail.com" }} onClose={onClose} onRetry={onRetry} />);
    expect(screen.getByTestId("link-done")).toHaveTextContent("Connected · james@gmail.com");
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalled();
    rerender(<LinkSheet service={null} phase={{ kind: "idle" }} onClose={onClose} onRetry={onRetry} />);
    expect(screen.queryByTestId("link-sheet")).toBeNull();
  });

  it("hostOf drops www and keeps a meaningful path", () => {
    expect(hostOf("https://www.google.com/device")).toBe("google.com/device");
    expect(hostOf("https://link.tidal.com/")).toBe("link.tidal.com");
    expect(hostOf("not a url")).toBe("not a url");
  });
});
