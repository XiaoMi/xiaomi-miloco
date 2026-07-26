export type OmniProviderPreset = {
  id: string;
  name: string;
  baseUrl: string;
  model: string;
};

export const OMNI_PROVIDER_PRESETS: OmniProviderPreset[] = [
  {
    id: "xiaomi-mimo",
    name: "Xiaomi MiMo",
    baseUrl: "https://api.xiaomimimo.com/v1",
    model: "xiaomi/mimo-v2.5",
  },
  {
    id: "atlas-cloud",
    name: "Atlas Cloud",
    baseUrl: "https://api.atlascloud.ai/v1",
    model: "xiaomi/mimo-v2.5",
  },
];

export function findOmniProviderPreset(
  baseUrl: string,
): OmniProviderPreset | undefined {
  const normalizeBaseUrl = (url: string) => url.trim().replace(/\/+$/, "");
  const normalizedBaseUrl = normalizeBaseUrl(baseUrl);
  return OMNI_PROVIDER_PRESETS.find(
    (preset) => normalizeBaseUrl(preset.baseUrl) === normalizedBaseUrl,
  );
}
