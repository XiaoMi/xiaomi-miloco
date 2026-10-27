import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";

const html = readFileSync(new URL("../public/watch.html", import.meta.url), "utf8");
const script = html.match(/<script type="module">([\s\S]*?)<\/script>/)![1];

/** Execute the shipped page, not a second implementation of its retry policy. */
function boot(language = "zh") {
  const elements = new Map<string, any>();
  function element() {
    const classes = new Set<string>();
    return {
      value: "", textContent: "", className: "", style: {}, width: 1280, height: 720,
      options: [] as any[], handlers: new Map<string, Function>(),
      classList: { add: (v: string) => classes.add(v), remove: (v: string) => classes.delete(v), contains: (v: string) => classes.has(v) },
      appendChild(opt: any) { this.options.push(opt); },
      addEventListener(type: string, fn: Function) { this.handlers.set(type, fn); },
      getContext: () => ({ clearRect() {}, drawImage() {} }),
      pause() {}, load() {}, removeAttribute() {},
    };
  }
  const get = (id: string) => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id)!;
  };
  get("ch").value = "0";
  const sockets: FakeSocket[] = [];
  class FakeSocket {
    static CONNECTING = 0; static OPEN = 1; static CLOSED = 3;
    readyState = FakeSocket.CONNECTING;
    handlers = new Map<string, Function[]>();
    constructor(public url: string) { sockets.push(this); }
    // Deliberately allow late callbacks even after AbortController.abort(), as
    // older webviews may ignore the listener's signal option.
    addEventListener(type: string, fn: Function) {
      this.handlers.set(type, [...(this.handlers.get(type) ?? []), fn]);
    }
    emit(type: string, data: object = {}) {
      for (const fn of this.handlers.get(type) ?? []) fn({ currentTarget: this, ...data });
    }
    open() { this.readyState = FakeSocket.OPEN; this.emit("open"); }
    close() { this.readyState = FakeSocket.CLOSED; }
    disconnect(code = 1006) { this.close(); this.emit("close", { code, reason: "" }); }
    message(data: any) { this.emit("message", { data }); }
  }
  const pages = new Map<string, Function>();
  const math = Object.create(Math);
  math.random = () => 0.5;
  const sandbox: any = {
    document: { getElementById: get, createElement: element, body: element(), documentElement: {} },
    location: { search: "?embedded=1&camera_id=cam1&token=test-only", protocol: "http:", host: "test.invalid" },
    localStorage: { getItem: (k: string) => k === "web:lang" ? language : null, setItem() {} },
    WebSocket: FakeSocket, URLSearchParams, AbortController, Uint8Array, DataView,
    Date, Math: math, performance, console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    addEventListener: (type: string, fn: Function) => pages.set(type, fn),
    // Decoder stubs only allow the real page to reach its successful frame callback.
    VideoDecoder: class {
      static async isConfigSupported() { return { supported: true }; }
      constructor(public callbacks: any) {}
      configure() {}
      decode() { this.callbacks.output({ displayWidth: 1280, displayHeight: 720, close() {} }); }
      close() {}
    },
    EncodedVideoChunk: class { constructor(_init: unknown) {} },
    HTMLVideoElement: class {},
  };
  sandbox.window = sandbox;
  const context = createContext(sandbox);
  runInContext(script, context);
  return { sockets, get, pages, context };
}

describe("watch.html abnormal-disconnect recovery", () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-27T00:00:00Z")); });
  afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

  it("recovers from VPN close 1006 with one delayed retry and a visible overlay", () => {
    const page = boot();
    page.get("loading").classList.add("hidden");
    page.sockets[0].disconnect();
    expect(page.get("loading").classList.contains("hidden")).toBe(false);
    expect(page.get("loading").textContent).toContain("1 秒后重连");
    vi.advanceTimersByTime(999);
    expect(page.sockets).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(page.sockets).toHaveLength(2);
    expect(page.sockets[1].url).toBe(page.sockets[0].url);
  });

  it("backs off even if each failed attempt completes the WS handshake", () => {
    const { sockets } = boot();
    for (const delay of [1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
      const count = sockets.length;
      sockets.at(-1)!.open();
      sockets.at(-1)!.disconnect();
      vi.advanceTimersByTime(delay - 1);
      expect(sockets).toHaveLength(count);
      vi.advanceTimersByTime(1);
      expect(sockets).toHaveLength(count + 1);
    }
  });

  it("resets backoff only after a decoded frame is displayed", async () => {
    const page = boot();
    page.sockets[0].disconnect();
    vi.advanceTimersByTime(1000);
    const socket = page.sockets[1];
    socket.open();
    socket.message(JSON.stringify({ type: "init", codec: "h264" }));
    const frame = new Uint8Array(21);
    frame[0] = 1;
    frame.set([0, 0, 0, 1, 0x65], 16);
    socket.message(frame.buffer);
    await vi.advanceTimersByTimeAsync(0);
    expect(page.get("loading").classList.contains("hidden")).toBe(true);
    socket.disconnect();
    vi.advanceTimersByTime(1000);
    expect(page.sockets).toHaveLength(3);
  });

  it.each([1001, 1005, 1011, 1012, 1013, 1014])("retries transient close %i", (code) => {
    const { sockets } = boot();
    sockets[0].disconnect(code);
    vi.advanceTimersByTime(1000);
    expect(sockets).toHaveLength(2);
  });

  it.each([1000, 1002, 1003, 1008])("does not retry terminal/normal close %i", (code) => {
    const { sockets } = boot();
    sockets[0].disconnect(code);
    vi.advanceTimersByTime(60000);
    expect(sockets).toHaveLength(1);
  });

  it("preserves an explicit camera error instead of retrying it", () => {
    const page = boot();
    page.sockets[0].message(JSON.stringify({ type: "error", reason: "camera_unreachable" }));
    page.sockets[0].disconnect(1011);
    vi.advanceTimersByTime(60000);
    expect(page.sockets).toHaveLength(1);
    expect(page.get("state").textContent).toContain("连不上摄像头");
  });

  it("manual camera/channel changes cancel a pending retry and reset backoff", () => {
    const page = boot();
    page.sockets[0].disconnect();
    page.get("cam").value = "cam2";
    page.get("ch").value = "1";
    page.get("cam").handlers.get("change")!({});
    expect(page.sockets).toHaveLength(2);
    expect(page.sockets[1].url).toContain("camera_id=cam2&channel=1");
    vi.advanceTimersByTime(1000);
    expect(page.sockets).toHaveLength(2);
    page.sockets[1].disconnect();
    vi.advanceTimersByTime(1000);
    expect(page.sockets).toHaveLength(3);
  });

  it("ignores late close callbacks from the previous connection", () => {
    const page = boot();
    const old = page.sockets[0];
    page.get("go").handlers.get("click")!({});
    old.disconnect();
    vi.advanceTimersByTime(1000);
    expect(page.sockets).toHaveLength(2);
    expect(page.get("state").textContent).toBe("连接中…");
  });

  it("does not enqueue duplicate retries for a repeated close callback", () => {
    const page = boot();
    const old = page.sockets[0];
    old.disconnect(); old.disconnect();
    vi.advanceTimersByTime(1000);
    expect(page.sockets).toHaveLength(2);
  });

  it.each([false, true])("recovers a handshake/no-message blackhole (open=%s)", (open) => {
    const { sockets } = boot();
    if (open) sockets[0].open();
    vi.advanceTimersByTime(17000);
    expect(sockets).toHaveLength(2);
    expect(sockets[0].readyState).toBe(3);
  });

  it("cancels pending work on pagehide and reconnects on bfcache restoration", () => {
    const page = boot();
    page.sockets[0].disconnect();
    page.pages.get("pagehide")!({});
    vi.advanceTimersByTime(60000);
    expect(page.sockets).toHaveLength(1);
    page.pages.get("pageshow")!({ persisted: true });
    expect(page.sockets).toHaveLength(2);
  });

  it("provides an English retry status", () => {
    const page = boot("en");
    page.sockets[0].disconnect();
    expect(page.get("loading").textContent).toContain("Retrying in 1s");
  });
});
