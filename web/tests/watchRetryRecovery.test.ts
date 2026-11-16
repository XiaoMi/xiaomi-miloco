import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";

const html = readFileSync(process.env.WATCH_HTML || new URL("../public/watch.html", import.meta.url), "utf8");
const script = html.match(/<script type="module">([\s\S]*?)<\/script>/)![1];

/** Execute the shipped page, not a second implementation of its retry policy. */
function boot(language = "zh", hidden = false) {
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
  const decoders: any[] = [];
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
  const documentEvents = new Map<string, Function>();
  const math = Object.create(Math);
  math.random = () => 0.5;
  const sandbox: any = {
    document: { hidden, getElementById: get, createElement: element, body: element(), documentElement: {},
      addEventListener: (type: string, fn: Function) => documentEvents.set(type, fn) },
    location: { search: "?embedded=1&camera_id=cam1&token=test-only", protocol: "http:", host: "test.invalid" },
    localStorage: { getItem: (k: string) => k === "web:lang" ? language : null, setItem() {} },
    WebSocket: FakeSocket, URLSearchParams, AbortController, Uint8Array, DataView,
    Date, Math: math, performance, console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    addEventListener: (type: string, fn: Function) => pages.set(type, fn),
    // Decoder stubs only allow the real page to reach its successful frame callback.
    VideoDecoder: class {
      static async isConfigSupported() { return { supported: true }; }
      constructor(public callbacks: any) { decoders.push(this); }
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
  return { sockets, get, pages, context, documentEvents, document: sandbox.document, decoders };
}


describe("temporary camera stream recovery", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });
  it.each([undefined, true])("retries no-first-frame errors (retryable=%s)", retryable => {
    const page = boot();
    page.sockets[0].message(JSON.stringify({ type: "error", reason: "camera_unreachable", retryable }));
    expect(page.get("state").textContent).not.toContain("离线");
    page.sockets[0].disconnect(1011);
    vi.advanceTimersByTime(1000);
    expect(page.sockets).toHaveLength(2);
  });
  it("backs off across successful handshakes without frames", () => {
    const { sockets } = boot();
    for (const delay of [1000, 2000, 4000, 8000, 16000, 30000]) {
      const count = sockets.length;
      sockets.at(-1)!.open();
      sockets.at(-1)!.disconnect(1006);
      vi.advanceTimersByTime(delay - 1);
      expect(sockets).toHaveLength(count);
      vi.advanceTimersByTime(1);
      expect(sockets).toHaveLength(count + 1);
    }
  });
  it.each(["camera_unreachable_cross_subnet", "unauthorized"])("preserves terminal %s errors", reason => {
    const page = boot();
    page.sockets[0].message(JSON.stringify({ type: "error", reason }));
    page.sockets[0].disconnect(1011);
    vi.advanceTimersByTime(60000);
    expect(page.sockets).toHaveLength(1);
  });
  it("cancels a queued retry on manual reconnect and ignores stale closes", () => {
    const page = boot();
    const old = page.sockets[0];
    old.disconnect(1006);
    page.get("go").handlers.get("click")!({});
    old.disconnect(1006);
    vi.advanceTimersByTime(1000);
    expect(page.sockets).toHaveLength(2);
  });
  it("cancels a queued retry when leaving the page", () => {
    const page = boot();
    page.sockets[0].disconnect(1006);
    page.pages.get("pagehide")!({});
    vi.advanceTimersByTime(60000);
    expect(page.sockets).toHaveLength(1);
  });
});

it("honors an explicit non-retryable server verdict", () => {
  vi.useFakeTimers();
  try {
    const page = boot();
    page.sockets[0].message(JSON.stringify({
      type: "error", reason: "camera_unreachable", retryable: false
    }));
    page.sockets[0].disconnect(1011);
    vi.advanceTimersByTime(60000);
    expect(page.sockets).toHaveLength(1);
  } finally {
    vi.clearAllTimers();
    vi.useRealTimers();
  }
});

it("does not diagnose handshake rejection as a camera failure", () => {
  vi.useFakeTimers();
  try {
    const page = boot();
    page.sockets[0].disconnect(1006);
    expect(page.get("state").textContent).toContain("授权");
    expect(page.get("state").textContent).not.toContain("画面");
    vi.advanceTimersByTime(1000);
    expect(page.sockets).toHaveLength(2);
  } finally { vi.clearAllTimers(); vi.useRealTimers(); }
});
