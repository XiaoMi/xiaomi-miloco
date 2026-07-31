import { describe, it, expect } from "vitest";
import type { ScopeCamera } from "@/lib/types";
import { sortCamerasByDid } from "@/components/PerceptionDeviceTable.helpers";

function cam(did: string, over: Partial<ScopeCamera> = {}): ScopeCamera {
  return {
    did, name: did, channel: 0, channelCount: 1,
    cloudOnline: true, lanReachable: true, awake: true,
    inUse: false, voiceInUse: false, connected: false,
    videoEnabled: false, audioEnabled: false,
    ...over,
  } as ScopeCamera;
}

describe("sortCamerasByDid", () => {
  it("按 did 升序", () => {
    expect(sortCamerasByDid([cam("c3"), cam("c1"), cam("c2")]).map(c => c.did))
      .toEqual(["c1", "c2", "c3"]);
  });
  it("不修改原数组", () => {
    const input = [cam("c3"), cam("c1")];
    sortCamerasByDid(input);
    expect(input.map(c => c.did)).toEqual(["c3", "c1"]);
  });
  it("空数组", () => {
    expect(sortCamerasByDid([])).toEqual([]);
  });
});
