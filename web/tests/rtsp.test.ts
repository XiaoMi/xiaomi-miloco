import { afterEach, describe, expect, it, vi } from "vitest";

import {
  realAddRtspCamera,
  realDeleteRtspCamera,
  realUpdateRtspCamera,
} from "@/api/real";
import { resolveRecorderEndpoints } from "@/components/MiotRecorder";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

function cameraResponse() {
  return new Response(
    JSON.stringify({
      code: 0,
      message: "ok",
      data: {
        did: "rtsp:front",
        name: "Front door",
        source: "rtsp",
        url: "rtsp://camera/live",
        room_name: "RTSP",
        is_online: true,
        in_use: true,
        connected: false,
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

describe("RTSP camera API", () => {
  it("creates and maps an RTSP camera", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) => cameraResponse(),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const camera = await realAddRtspCamera({
      name: "Front door",
      url: "rtsp://camera/live",
    });

    expect(camera).toMatchObject({
      did: "rtsp:front",
      source: "rtsp",
      cloudOnline: true,
      lanReachable: true,
      voiceInUse: false,
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/miot/rtsp_cameras");
    expect(init).toMatchObject({ method: "POST" });
  });

  it("encodes IDs for update and delete", async () => {
    const fetchMock = vi
      .fn(async (_input: RequestInfo | URL, _init?: RequestInit) => cameraResponse())
      .mockResolvedValueOnce(cameraResponse())
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ code: 0, message: "ok", data: null }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    await realUpdateRtspCamera("rtsp:front/1", {
      name: "Front door",
      url: "rtsp://camera/live",
    });
    await realDeleteRtspCamera("rtsp:front/1");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/miot/rtsp_cameras/rtsp%3Afront%2F1",
    );
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "PUT" });
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: "DELETE" });
  });
});

describe("RTSP recorder routing", () => {
  it("uses MJPEG preview and the RTSP recording endpoint", () => {
    expect(resolveRecorderEndpoints("rtsp:front", 15_000)).toEqual({
      previewUrl: "/api/miot/rtsp_cameras/rtsp%3Afront/mjpeg",
      recordUrl:
        "/api/miot/rtsp_cameras/rtsp%3Afront/record_clip?duration_ms=15000",
      previewKind: "mjpeg",
    });
  });

  it("keeps MIoT channel routing unchanged", () => {
    expect(resolveRecorderEndpoints("camera:ch1", 15_000)).toEqual({
      previewUrl: "/api/miot/watch?camera_id=camera&channel=1&embedded=1",
      recordUrl:
        "/api/miot/record_clip?camera_id=camera&channel=1&duration_ms=15000",
      previewKind: "iframe",
    });
  });
});
