import { fireEvent, render, screen, waitFor } from "@testing-library/react";

/** jsdom does not run a submit button's activation behaviour for a dispatched click; submit the form. */
function submit(el: HTMLElement) {
  fireEvent.submit(el.closest("form")!);
}
import { axe } from "vitest-axe";
import { HUB_ADDRESS_CONNECT, HUB_ADDRESS_FORGET, HUB_ADDRESS_TITLE, HUB_ADDRESS_USE, HubAddressScreen, foundLine } from "./HubAddressScreen";
import { NativeGate } from "./NativeGate";
import { HUB_ADDRESS_SAVE_FAILED, HUB_BASE_KEY } from "@/lib/hub/hubBase";

const okFetch = vi.fn(async () => new Response(JSON.stringify({ version: "0.9.0" }), { status: 200 }));
const downFetch = vi.fn(async () => {
  throw new TypeError("Failed to fetch");
});

describe("HubAddressScreen", () => {
  beforeEach(() => {
    localStorage.clear();
    delete window.__ILLYHUB_NATIVE__;
  });

  it("asks where the hub is, connects, and stores the address on confirm (axe clean)", async () => {
    const onSaved = vi.fn();
    const { container } = render(<HubAddressScreen onSaved={onSaved} fetcher={okFetch as unknown as typeof fetch} />);
    expect(screen.getByRole("heading", { level: 1, name: HUB_ADDRESS_TITLE })).toBeInTheDocument();
    const input = screen.getByTestId("hub-address-input");
    expect(input).toHaveFocus();
    const connect = screen.getByTestId("hub-address-connect");
    expect(connect).toBeDisabled();

    fireEvent.change(input, { target: { value: "192.168.50.10" } });
    expect(screen.getByTestId("hub-address-preview")).toHaveTextContent("Will connect to http://192.168.50.10:8080");
    expect(connect).toBeEnabled();
    expect(connect).toHaveTextContent(HUB_ADDRESS_CONNECT);

    submit(connect);
    await waitFor(() => expect(screen.getByTestId("hub-address-status")).toHaveTextContent(foundLine("http://192.168.50.10:8080", "0.9.0")));
    expect(screen.getByTestId("hub-address-status")).toHaveAttribute("role", "status");
    expect(await axe(container)).toHaveNoViolations();

    submit(screen.getByTestId("hub-address-use"));
    expect(localStorage.getItem(HUB_BASE_KEY)).toBe("http://192.168.50.10:8080");
    expect(onSaved).toHaveBeenCalledWith("http://192.168.50.10:8080");
    expect(screen.getByTestId("hub-address-use")).toHaveTextContent(HUB_ADDRESS_USE);
  });

  it("shows one factual error line when the hub cannot be reached, and clears it on edit", async () => {
    render(<HubAddressScreen onSaved={vi.fn()} fetcher={downFetch as unknown as typeof fetch} />);
    const input = screen.getByTestId("hub-address-input");
    fireEvent.change(input, { target: { value: "192.168.50.99" } });
    submit(screen.getByTestId("hub-address-connect"));
    await waitFor(() => expect(screen.getByTestId("hub-address-status")).toHaveAttribute("role", "alert"));
    expect(screen.getByTestId("hub-address-status")).toHaveTextContent("Couldn't reach http://192.168.50.99:8080. Check the address and that you're on the same Wi-Fi.");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(screen.queryByTestId("hub-address-use")).toBeNull();
    fireEvent.change(input, { target: { value: "192.168.50.10" } });
    expect(screen.getByTestId("hub-address-status")).toHaveTextContent("");
    expect(input).not.toHaveAttribute("aria-invalid");
  });

  it("ignores a stale probe when the address changes mid-flight", async () => {
    let resolveFirst: (r: Response) => void = () => {};
    const slowThenFast = vi
      .fn()
      .mockImplementationOnce(() => new Promise<Response>((res) => (resolveFirst = res)))
      .mockImplementationOnce(async () => new Response(JSON.stringify({ version: "2" }), { status: 200 }));
    render(<HubAddressScreen onSaved={vi.fn()} fetcher={slowThenFast as unknown as typeof fetch} />);
    const input = screen.getByTestId("hub-address-input");
    fireEvent.change(input, { target: { value: "10.0.0.1" } });
    submit(screen.getByTestId("hub-address-connect"));
    fireEvent.change(input, { target: { value: "10.0.0.2" } });
    submit(screen.getByTestId("hub-address-connect"));
    await waitFor(() => expect(screen.getByTestId("hub-address-status")).toHaveTextContent("Found the hub at http://10.0.0.2:8080 · version 2"));
    resolveFirst(new Response(JSON.stringify({ version: "1" }), { status: 200 }));
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByTestId("hub-address-status")).toHaveTextContent("version 2");
  });

  it("does not reload when the address cannot be stored; says so instead", async () => {
    const onSaved = vi.fn();
    render(<HubAddressScreen onSaved={onSaved} fetcher={okFetch as unknown as typeof fetch} />);
    fireEvent.change(screen.getByTestId("hub-address-input"), { target: { value: "192.168.50.10" } });
    submit(screen.getByTestId("hub-address-connect"));
    await waitFor(() => expect(screen.getByTestId("hub-address-use")).toBeInTheDocument());
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    submit(screen.getByTestId("hub-address-use"));
    spy.mockRestore();
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByTestId("hub-address-status")).toHaveTextContent(HUB_ADDRESS_SAVE_FAILED);
    expect(screen.getByTestId("hub-address-status")).toHaveAttribute("role", "alert");
  });

  it("from Settings: prefilled, Cancel and Forget are offered, Forget clears storage", () => {
    localStorage.setItem(HUB_BASE_KEY, "http://192.168.50.10:8080");
    const onCancel = vi.fn();
    const onForget = vi.fn();
    render(<HubAddressScreen onSaved={vi.fn()} onCancel={onCancel} onForget={onForget} embedded />);
    expect(screen.getByTestId("hub-address-input")).toHaveValue("http://192.168.50.10:8080");
    expect(screen.queryByRole("heading")).toBeNull(); // the sheet owns the one heading
    expect(screen.getByTestId("hub-address-input")).not.toHaveFocus(); // the sheet owns focus
    fireEvent.click(screen.getByTestId("hub-address-cancel"));
    expect(onCancel).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: HUB_ADDRESS_FORGET }));
    expect(localStorage.getItem(HUB_BASE_KEY)).toBeNull();
    expect(onForget).toHaveBeenCalled();
  });
});

describe("NativeGate", () => {
  beforeEach(() => {
    localStorage.clear();
    delete window.__ILLYHUB_NATIVE__;
  });

  it("renders the app in a browser", () => {
    render(
      <NativeGate>
        <p>app</p>
      </NativeGate>,
    );
    expect(screen.getByText("app")).toBeInTheDocument();
    expect(screen.queryByTestId("hub-address")).toBeNull();
  });

  it("asks for the hub address inside the native shell until one is stored", () => {
    window.__ILLYHUB_NATIVE__ = true;
    const { unmount } = render(
      <NativeGate>
        <p>app</p>
      </NativeGate>,
    );
    expect(screen.getByTestId("hub-address")).toBeInTheDocument();
    expect(screen.queryByText("app")).toBeNull();
    unmount();
    localStorage.setItem(HUB_BASE_KEY, "http://192.168.50.10:8080");
    render(
      <NativeGate>
        <p>app</p>
      </NativeGate>,
    );
    expect(screen.getByText("app")).toBeInTheDocument();
  });
});
