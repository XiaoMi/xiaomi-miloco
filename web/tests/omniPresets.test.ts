import { describe, expect, it } from "vitest";
import {
  findOmniProviderPreset,
  OMNI_PROVIDER_PRESETS,
} from "../src/lib/omniPresets";

describe("omni provider presets", () => {
  it("configures Atlas Cloud with the MiMo multimodal model", () => {
    expect(OMNI_PROVIDER_PRESETS).toContainEqual({
      id: "atlas-cloud",
      name: "Atlas Cloud",
      baseUrl: "https://api.atlascloud.ai/v1",
      model: "xiaomi/mimo-v2.5",
    });
  });

  it("matches normalized provider settings", () => {
    expect(findOmniProviderPreset(" https://api.atlascloud.ai/v1/ ")?.id).toBe(
      "atlas-cloud",
    );
  });

  it("keeps unknown settings custom", () => {
    expect(findOmniProviderPreset("https://example.com/v1")).toBeUndefined();
  });
});
