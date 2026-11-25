import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it } from "vitest";

// Run the actual standalone watch-page function without booting its WebSocket.
const source = readFileSync(new URL("../public/watch.html", import.meta.url), "utf8");
const handler = source.slice(source.indexOf("function showLiveFps(msg)"), source.indexOf("function setState("));

describe("watch live fps badge", () => {
  it("updates each partial restore and hides only at the requested rate", () => {
    const classes = new Set<string>();
    const el = {
      textContent: "",
      classList: { add: (v: string) => classes.add(v), remove: (v: string) => classes.delete(v) },
    };
    const show = runInNewContext(`${handler}\nshowLiveFps`, {
      $: () => el,
      t: () => "now {fps} fps / max {max}",
    });
    show({ reason: "degraded", fps: 1, requested: 20 });
    expect(el.textContent).toBe("now 1 fps / max 20");
    for (const fps of [3, 5, 10]) {
      show({ reason: "restored", fps, requested: 20 });
      expect(el.textContent).toBe(`now ${fps} fps / max 20`);
      expect(classes.has("show")).toBe(true);
    }
    show({ reason: "restored", fps: 20, requested: 20 });
    expect(classes.has("show")).toBe(false);
  });

  it("tolerates a missing badge element", () => {
    const show = runInNewContext(`${handler}\nshowLiveFps`, { $: () => null });
    expect(() => show({ reason: "restored", fps: 3, requested: 20 })).not.toThrow();
  });
});
