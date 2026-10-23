import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_LIVE_FPS,
  LIVE_FPS_KEY,
  LIVE_FPS_OPTIONS,
  normalizeLiveFps,
  readLiveFps,
  saveLiveFps,
} from "@/lib/liveFps";

function memoryStorage() {
  const data = new Map<string, string>();
  return {
    getItem: (k: string) => (data.has(k) ? (data.get(k) as string) : null),
    setItem: (k: string, v: string) => void data.set(k, String(v)),
    removeItem: (k: string) => void data.delete(k),
  };
}

describe("liveFps", () => {
  beforeEach(() => vi.stubGlobal("localStorage", memoryStorage()));
  afterEach(() => vi.unstubAllGlobals());

  it("offers low rates first and defaults to 5 fps", () => {
    expect([...LIVE_FPS_OPTIONS]).toEqual([1, 3, 5, 10, 20]);
    expect(DEFAULT_LIVE_FPS).toBe(5);
  });

  it("normalizes unknown values to the default", () => {
    expect(normalizeLiveFps(10)).toBe(10);
    expect(normalizeLiveFps("3")).toBe(3);
    expect(normalizeLiveFps(7)).toBe(5);
    expect(normalizeLiveFps(null)).toBe(5);
    expect(normalizeLiveFps("abc")).toBe(5);
  });

  it("persists the choice in localStorage", () => {
    expect(readLiveFps()).toBe(5);
    saveLiveFps(20);
    expect(localStorage.getItem(LIVE_FPS_KEY)).toBe("20");
    expect(readLiveFps()).toBe(20);
    saveLiveFps(99);
    expect(readLiveFps()).toBe(5);
  });

  it("falls back to the default when storage is unavailable", () => {
    vi.stubGlobal("localStorage", undefined);
    expect(readLiveFps()).toBe(5);
    expect(() => saveLiveFps(10)).not.toThrow();
  });
});
