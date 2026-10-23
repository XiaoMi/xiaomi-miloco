import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DEVICE_DIRECTORY_KEY_PREFIX,
  readDeviceDirectory,
  saveDeviceDirectory,
} from "@/lib/deviceDirectoryCache";

function memoryStorage() {
  const data = new Map<string, string>();
  return {
    getItem: (k: string) => (data.has(k) ? (data.get(k) as string) : null),
    setItem: (k: string, v: string) => void data.set(k, String(v)),
    removeItem: (k: string) => void data.delete(k),
  };
}

const entries = [
  { did: "cam-1", name: "客厅摄像头", room: "客厅" },
  { did: "lamp-1", name: "Lamp", room: "" },
];

describe("deviceDirectoryCache", () => {
  beforeEach(() => vi.stubGlobal("localStorage", memoryStorage()));
  afterEach(() => vi.unstubAllGlobals());

  it("returns undefined when nothing is cached", () => {
    expect(readDeviceDirectory("home-1")).toBeUndefined();
  });

  it("round-trips the last successful directory per home", () => {
    saveDeviceDirectory("home-1", entries);
    expect(readDeviceDirectory("home-1")).toEqual(entries);
    expect(readDeviceDirectory("home-2")).toBeUndefined();
  });

  it("keeps only did, name and room", () => {
    saveDeviceDirectory("home-1", [{ ...entries[0], token: "x" } as never]);
    const raw = localStorage.getItem(DEVICE_DIRECTORY_KEY_PREFIX + "home-1") ?? "";
    expect(raw).not.toContain("token");
  });

  it("ignores corrupt or malformed cache", () => {
    localStorage.setItem(DEVICE_DIRECTORY_KEY_PREFIX + "home-1", "{not json");
    expect(readDeviceDirectory("home-1")).toBeUndefined();
    localStorage.setItem(DEVICE_DIRECTORY_KEY_PREFIX + "home-1", JSON.stringify([{ did: 1 }]));
    expect(readDeviceDirectory("home-1")).toBeUndefined();
  });

  it("works without localStorage", () => {
    vi.stubGlobal("localStorage", undefined);
    expect(() => saveDeviceDirectory("home-1", entries)).not.toThrow();
    expect(readDeviceDirectory("home-1")).toBeUndefined();
  });
});
