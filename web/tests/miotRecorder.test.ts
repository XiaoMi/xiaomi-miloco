import { describe, expect, it } from "vitest";
import { resolveRecorderEndpoints } from "@/components/MiotRecorder";

describe("resolveRecorderEndpoints", () => {
  it("routes MiOT cameras to existing watch and record endpoints", () => {
    const endpoints = resolveRecorderEndpoints("miot-cam-1", 0, 15000);

    expect(endpoints.previewUrl).toBe(
      "/api/miot/watch?camera_id=miot-cam-1&channel=0&embedded=1",
    );
    expect(endpoints.recordUrl).toBe(
      "/api/miot/record_clip?camera_id=miot-cam-1&channel=0&duration_ms=15000",
    );
  });

  it("routes RTSP cameras away from MiOT SDK record_clip", () => {
    const endpoints = resolveRecorderEndpoints("rtsp:9c17b9414fe9", 0, 15000);

    expect(endpoints.previewUrl).toBe(
      "/api/miot/rtsp_cameras/rtsp%3A9c17b9414fe9/mjpeg",
    );
    expect(endpoints.recordUrl).toBe(
      "/api/miot/rtsp_cameras/rtsp%3A9c17b9414fe9/record_clip?duration_ms=15000",
    );
    expect(endpoints.recordUrl).not.toContain("/api/miot/record_clip");
  });
});
