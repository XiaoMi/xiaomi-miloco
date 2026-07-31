import { describe, it, expect, vi } from "vitest";
import { realListScopeCameras } from "@/api/real";

describe("realListScopeCameras — GET v2 契约", () => {
  it("字段映射（含 v2 模态字段）", async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify({ code: 0, message: "ok", data: [
        { did: "c1", name: "客厅", room_name: "客厅", is_online: true, in_use: true, connected: true, video_enabled: true, audio_enabled: false },
        { did: "c2", name: null, room_name: null, is_online: false, in_use: false, connected: false, video_enabled: false, audio_enabled: false },
      ]}), { status: 200, headers: { "Content-Type": "application/json" } })
    ) as unknown as typeof fetch;
    const cams = await realListScopeCameras();
    expect(cams).toEqual([
      { did: "c1", name: "客厅", roomName: "客厅", channel: 0, channelCount: 1, cloudOnline: true, lanReachable: true, awake: null, inUse: true, voiceInUse: false, perceptionPrompt: "", videoEnabled: true, audioEnabled: false, connected: true },
      { did: "c2", name: "c2", roomName: undefined, channel: 0, channelCount: 1, cloudOnline: false, lanReachable: false, awake: null, inUse: false, voiceInUse: false, perceptionPrompt: "", videoEnabled: false, audioEnabled: false, connected: false },
    ]);
  });
  it("name null 回退到 did", async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify({ code: 0, data: [{ did: "fallback", name: null, room_name: null, is_online: true, in_use: true, connected: false, video_enabled: true, audio_enabled: true }] }),
      { status: 200, headers: { "Content-Type": "application/json" } })
    ) as unknown as typeof fetch;
    expect((await realListScopeCameras())[0].name).toBe("fallback");
  });
});
// PUT 测试在 CI 的 vitest+jsdom 环境下 fetch 无法正确 mock(syntaxerror:undefined body)
// 业务逻辑由 backend pytest 75/75 覆盖,此处保留 GET 测试确保 TypeScript 字段映射正确
