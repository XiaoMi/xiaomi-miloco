import { describe, expect, it } from "vitest";
import {
  groupDevicesByCategory,
  sortDevicesForDisplay,
} from "@/components/DevicesByRoom";
import type { Device } from "@/lib/types";

const dev = (overrides: Partial<Device>): Device => ({
  did: "d",
  name: "设备",
  category: "other",
  room: "客厅",
  online: true,
  statusText: "已连接",
  dangerous: false,
  mainSwitch: undefined,
  props: [],
  ...overrides,
});

describe("DevicesByRoom device grouping", () => {
  it("sorts online devices before offline devices", () => {
    const out = sortDevicesForDisplay([
      dev({ did: "offline", online: false }),
      dev({ did: "online", online: true }),
    ]);

    expect(out.map((d) => d.did)).toEqual(["online", "offline"]);
  });

  it("uses rawCategory before falling back to normalized category", () => {
    const groups = groupDevicesByCategory([
      dev({ did: "switch", category: "light", rawCategory: "wall-switch" }),
    ]);

    expect(groups[0][0]).toBe("plug_switch");
  });

  it("keeps groups with online devices before all-offline groups", () => {
    const groups = groupDevicesByCategory([
      dev({ did: "offline-camera", category: "camera", online: false }),
      dev({ did: "online-light", category: "light", online: true }),
    ]);

    expect(groups.map(([group]) => group)).toEqual(["lighting", "security"]);
  });

  it("sorts devices stably by localized name then did inside a group", () => {
    const out = sortDevicesForDisplay([
      dev({ did: "b", name: "台灯" }),
      dev({ did: "a", name: "台灯" }),
      dev({ did: "c", name: "壁灯" }),
    ]);

    expect(out.map((d) => d.did)).toEqual(["c", "a", "b"]);
  });
});
