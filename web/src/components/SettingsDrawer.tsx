import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getPerceptionConfig,
  updatePerceptionConfig,
  pausePerception,
  resumePerception,
  type PerceptionConfig,
} from "@/api";
import { useEscClose } from "@/hooks/useEscClose";
import { toast } from "./Toast";

// 与 backend settings.yaml perception.engine.input + perception.collect.window_size 对齐
const DEFAULTS: PerceptionConfig = { video_short_edge: 512, omni_fps: 1, window_size: 4 };

const SHORT_EDGE_OPTIONS = [360, 512, 768, 1080] as const;
const FPS_OPTIONS = [1, 2, 3] as const;
const WINDOW_MIN = 2;
const WINDOW_MAX = 10;

interface Props {
  open: boolean;
  onClose: () => void;
}

export function SettingsDrawer({ open, onClose }: Props) {
  const { t } = useTranslation();
  const [config, setConfig] = useState<PerceptionConfig | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const [videoShortEdge, setVideoShortEdge] = useState(DEFAULTS.video_short_edge);
  const [omniFps, setOmniFps] = useState(DEFAULTS.omni_fps);
  const [windowSize, setWindowSize] = useState(DEFAULTS.window_size);

  useEscClose(open, onClose);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    getPerceptionConfig()
      .then((c) => {
        setConfig(c);
        setVideoShortEdge(c.video_short_edge);
        setOmniFps(c.omni_fps);
        setWindowSize(c.window_size);
      })
      .catch(() => toast(t("settings.loadFailed"), "danger"))
      .finally(() => setLoading(false));
  }, [open, t]);

  async function handleSaveAndRestart() {
    setBusy(true);
    try {
      const updated = await updatePerceptionConfig({
        video_short_edge: videoShortEdge,
        omni_fps: omniFps,
        window_size: windowSize,
      });
      setConfig(updated);
      try {
        await pausePerception();
        await resumePerception();
        toast(t("settings.applySuccess"), "ok");
        onClose();
      } catch {
        toast(t("settings.restartFailed"), "warn");
      }
    } catch {
      toast(t("settings.saveFailed"), "danger");
    } finally {
      setBusy(false);
    }
  }

  function handleReset() {
    setVideoShortEdge(DEFAULTS.video_short_edge);
    setOmniFps(DEFAULTS.omni_fps);
    setWindowSize(DEFAULTS.window_size);
  }

  const dirty =
    config != null &&
    (videoShortEdge !== config.video_short_edge ||
      omniFps !== config.omni_fps ||
      windowSize !== config.window_size);

  if (!open) return null;

  return (
    <>
      <div
        className="fixed inset-0 z-[50] bg-black/40 transition-opacity"
        onClick={onClose}
      />
      <div className="fixed right-0 top-0 bottom-0 z-[51] w-80 max-w-[90vw] bg-bg-secondary border-l border-border shadow-xl flex flex-col">
        {/* header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-border">
          <h2 className="text-lg font-semibold text-text-primary">
            {t("settings.title")}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-text-tertiary hover:text-text-primary p-1"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* body */}
        <div className="flex-1 overflow-y-auto px-5 py-6 space-y-7">
          {loading ? (
            <div className="text-caption text-text-tertiary text-center py-8">
              Loading…
            </div>
          ) : (
            <>
              {/* 分辨率 */}
              <div className="space-y-2.5">
                <label className="text-body font-medium text-text-primary block">
                  {t("settings.videoShortEdge")}
                </label>
                <div className="flex gap-2">
                  {SHORT_EDGE_OPTIONS.map((v) => (
                    <button
                      key={v}
                      type="button"
                      onClick={() => setVideoShortEdge(v)}
                      className={`flex-1 py-2.5 rounded-xl text-body transition-colors ${
                        videoShortEdge === v
                          ? "bg-brand-primary text-white shadow-sm"
                          : "bg-bg-primary border border-border text-text-primary hover:border-brand-primary"
                      }`}
                    >
                      {v}p
                    </button>
                  ))}
                </div>
                <p className="text-caption text-text-tertiary">
                  {t("settings.videoShortEdgeHint")}
                </p>
              </div>

              {/* 帧率 */}
              <div className="space-y-2.5">
                <label className="text-body font-medium text-text-primary block">
                  {t("settings.omniFps")}
                </label>
                <div className="flex gap-2">
                  {FPS_OPTIONS.map((v) => (
                    <button
                      key={v}
                      type="button"
                      onClick={() => setOmniFps(v)}
                      className={`flex-1 py-2.5 rounded-xl text-body transition-colors ${
                        omniFps === v
                          ? "bg-brand-primary text-white shadow-sm"
                          : "bg-bg-primary border border-border text-text-primary hover:border-brand-primary"
                      }`}
                    >
                      {v} fps
                    </button>
                  ))}
                </div>
              </div>

              {/* 感知窗口 */}
              <div className="space-y-2.5">
                <div className="flex items-center justify-between">
                  <label className="text-body font-medium text-text-primary">
                    {t("settings.windowSize")}
                  </label>
                  <span className="text-body text-text-primary font-semibold">
                    {windowSize} {t("settings.windowSizeUnit")}
                  </span>
                </div>
                <input
                  type="range"
                  min={WINDOW_MIN}
                  max={WINDOW_MAX}
                  step={1}
                  value={windowSize}
                  onChange={(e) => setWindowSize(Number(e.target.value))}
                  className="settings-slider w-full"
                  style={{
                    background: `linear-gradient(to right, var(--color-brand-primary, #ff6900) ${((windowSize - WINDOW_MIN) / (WINDOW_MAX - WINDOW_MIN)) * 100}%, var(--color-border, #e5e5e5) ${((windowSize - WINDOW_MIN) / (WINDOW_MAX - WINDOW_MIN)) * 100}%)`,
                  }}
                />
                <div className="flex justify-between text-caption text-text-tertiary">
                  <span>{WINDOW_MIN} {t("settings.windowSizeUnit")}</span>
                  <span>{WINDOW_MAX} {t("settings.windowSizeUnit")}</span>
                </div>
              </div>

              {/* 恢复默认 */}
              <div className="flex justify-end">
                <button
                  type="button"
                  onClick={handleReset}
                  className="text-caption text-text-tertiary hover:text-text-primary transition-colors"
                >
                  {t("settings.resetDefaults")}
                </button>
              </div>
            </>
          )}
        </div>

        {/* footer */}
        <div className="px-5 py-4 border-t border-border">
          <button
            type="button"
            onClick={handleSaveAndRestart}
            disabled={busy || !dirty}
            className="w-full px-4 py-3 rounded-xl bg-brand-primary text-white text-body font-medium hover:opacity-90 disabled:opacity-60 transition-opacity"
          >
            {busy ? t("settings.applying") : t("settings.apply")}
          </button>
        </div>
      </div>
    </>
  );
}
