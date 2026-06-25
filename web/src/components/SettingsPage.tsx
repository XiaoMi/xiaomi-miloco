/**
 * ⚙️ 设置页 — 音视频质量预设切换。
 *
 * 提供 default / high 两个预设的可视化切换，切换后需重启服务生效。
 */

import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { apiFetch } from "@/api/client";

interface QualityParams {
  video_short_edge: number;
  audio_sample_rate: number;
  camera_video_quality: string;
}

interface QualityData {
  current: string;
  params: QualityParams;
  presets: Record<string, QualityParams>;
}

interface NormalResponse {
  code: number;
  message: string;
  data: QualityData | null;
}

export function SettingsPage() {
  const { t } = useTranslation();
  const [data, setData] = useState<QualityData | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  const fetchQuality = async () => {
    try {
      setLoading(true);
      const json = await apiFetch<NormalResponse>("/api/admin/perception-quality");
      if (json.code === 0 && json.data) {
        setData(json.data);
        setError(null);
      } else {
        setError(json.message);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : t("settings.loadFailed", "加载失败"));
    } finally {
      setLoading(false);
    }
  };

  const setPreset = async (preset: string) => {
    try {
      setSaving(true);
      setError(null);
      setSuccessMsg(null);
      const json = await apiFetch<NormalResponse>("/api/admin/perception-quality", {
        method: "PUT",
        body: JSON.stringify({ preset }),
      });
      if (json.code === 0 && json.data) {
        setData((prev) =>
          prev ? { ...prev, current: preset, params: json.data!.params } : prev
        );
        setSuccessMsg(json.message);
      } else {
        setError(json.message);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : t("settings.setFailed", "设置失败"));
    } finally {
      setSaving(false);
    }
  };

  useEffect(() => {
    fetchQuality();
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="text-text-secondary">{t("common.loading")}</div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* 音视频质量预设 */}
      <section className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6">
        <h2 className="text-section-title mb-2">
          {t("settings.qualityTitle", "音视频质量")}
        </h2>
        <p className="text-body text-text-secondary mb-4">
          {t(
            "settings.qualityDesc",
            "切换感知引擎的音视频质量预设。高质量模式可大幅提升识别精度，但会增加 Token 消耗。切换后需重启服务生效。"
          )}
        </p>

        {error && (
          <div className="mb-4 p-3 rounded-lg bg-error/10 text-error text-sm">
            {error}
          </div>
        )}
        {successMsg && (
          <div className="mb-4 p-3 rounded-lg bg-success/10 text-success text-sm">
            {successMsg}
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* Default 预设 */}
          <PresetCard
            label={t("settings.presetDefault", "默认模式")}
            description={t(
              "settings.presetDefaultDesc",
              "省 Token，适合日常监控"
            )}
            params={data?.presets?.default}
            active={data?.current === "default"}
            saving={saving}
            onSelect={() => setPreset("default")}
            t={t}
          />

          {/* High 预设 */}
          <PresetCard
            label={t("settings.presetHigh", "高质量模式")}
            description={t(
              "settings.presetHighDesc",
              "高清晰度，识别更精准，Token ~4.5x"
            )}
            params={data?.presets?.high}
            active={data?.current === "high"}
            saving={saving}
            onSelect={() => setPreset("high")}
            t={t}
          />
        </div>
      </section>
    </div>
  );
}

function PresetCard({
  label,
  description,
  params,
  active,
  saving,
  onSelect,
  t,
}: {
  label: string;
  description: string;
  params?: QualityParams;
  active: boolean;
  saving: boolean;
  onSelect: () => void;
  t: ReturnType<typeof useTranslation>["t"];
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      disabled={active || saving}
      className={`
        relative text-left p-4 rounded-xl border-2 transition-all
        ${
          active
            ? "border-primary bg-primary/5 shadow-sm"
            : "border-border hover:border-border-strong hover:bg-bg-tertiary"
        }
        ${saving ? "opacity-60 cursor-wait" : ""}
      `}
    >
      {active && (
        <span className="absolute top-3 right-3 text-xs font-medium text-primary bg-primary/10 px-2 py-0.5 rounded-full">
          {t("settings.currentBadge", "当前")}
        </span>
      )}
      <h3 className="text-body font-semibold mb-1">{label}</h3>
      <p className="text-caption text-text-secondary mb-3">{description}</p>
      {params && (
        <div className="space-y-1 text-caption-mono text-text-tertiary">
          <div>
            {t("settings.videoLabel", "视频: {{quality}} / 短边 {{edge}}px", {
              quality: params.camera_video_quality,
              edge: params.video_short_edge,
            })}
          </div>
          <div>
            {t("settings.audioLabel", "音频: {{rate}}kHz", {
              rate: params.audio_sample_rate / 1000,
            })}
          </div>
        </div>
      )}
    </button>
  );
}
