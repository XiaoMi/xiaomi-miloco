import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getPerceptionConfig,
  getSchedulerConfig,
  updatePerceptionConfig,
  updateSchedulerConfig,
  type PerceptionConfig,
} from "@/api";
import { useEscClose } from "@/hooks/useEscClose";
import { toast } from "./Toast";

// 与 backend settings.yaml perception.engine.input + perception.collect.window_size 对齐
const DEFAULTS: PerceptionConfig = { video_short_edge: 512, omni_fps: 1, window_size: 4, transmission_mode: "video" };

const SHORT_EDGE_OPTIONS = [360, 512, 768, 1080] as const;
const FPS_OPTIONS = [1, 2, 3] as const;
const TRANSMISSION_MODES = ["video", "screenshot"] as const;
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
  const [transmissionMode, setTransmissionMode] = useState<"video" | "screenshot">(DEFAULTS.transmission_mode);

  // 内置定时任务自动管理开关（scheduler.enabled）。缺省 true = 自动管理。
  const [schedulerLoaded, setSchedulerLoaded] = useState<boolean | null>(null);
  const [schedulerEnabled, setSchedulerEnabled] = useState(true);

  useEscClose(open, onClose);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    // 抽屉靠 `if (!open) return null` 隐藏而非卸载，state 会跨「关闭→重开」保留。
    // 每次重载先把调度值复位为 null：本次读不到就稳定退回 unavailable（disable 开关），
    // 不留用上次会话的旧值，与 e107541 的「读不到就禁用」保持一致。
    setSchedulerLoaded(null);
    // 感知参数与调度开关是两个正交接口，用 allSettled 各自独立成败——
    // 任一接口出错（如版本错位）只影响自己那块，不把另一块也拖进错误态。
    Promise.allSettled([
      getPerceptionConfig().then((c) => {
        setConfig(c);
        setVideoShortEdge(c.video_short_edge);
        setOmniFps(c.omni_fps);
        setWindowSize(c.window_size);
        setTransmissionMode(c.transmission_mode ?? "video");
      }),
      getSchedulerConfig().then((s) => {
        setSchedulerLoaded(s.enabled);
        setSchedulerEnabled(s.enabled);
      }),
    ])
      .then((rs) => {
        // 只有感知参数（rs[0]，核心设置）加载失败才报错；调度开关（rs[1]）失败
        // 已由 schedulerLoaded=null 优雅降级为「不可配置」（置灰 + 专属 hint），
        // 不应再弹「加载设置失败」——否则老后端(无 /scheduler-config)每次打开
        // 设置都会看到与降级体验自相矛盾的误导性红条。
        if (rs[0].status === "rejected") {
          toast(t("settings.loadFailed"), "danger");
        }
      })
      .finally(() => setLoading(false));
  }, [open, t]);

  const perceptionDirty =
    config != null &&
    (videoShortEdge !== config.video_short_edge ||
      omniFps !== config.omni_fps ||
      windowSize !== config.window_size ||
      transmissionMode !== (config.transmission_mode ?? "video"));
  // schedulerLoaded === null 表示这次没读到服务端值（接口缺失 / 版本错位）：
  // 此时 schedulerDirty 恒 false，拨动开关不会写盘，故置灰禁用避免呈现「看着能动、
  // 实则静默丢弃」的控件。
  const schedulerAvailable = schedulerLoaded != null;
  const schedulerDirty =
    schedulerAvailable && schedulerEnabled !== schedulerLoaded;

  async function handleSaveAndRestart() {
    setBusy(true);
    // 调度开关先于感知参数提交；记录其是否已写盘，供 catch 区分「部分成功」——
    // 开关已存但感知失败时不应笼统报「保存失败」，那会让用户误以为开关也没存住。
    let schedulerSaved = false;
    try {
      // scheduler 开关仅写盘 config.json（agent 网关下次启动读取生效），
      // 与感知参数各自独立 PUT——只在各自变更时提交，避免仅改开关却重启引擎。
      if (schedulerDirty) {
        const s = await updateSchedulerConfig({ enabled: schedulerEnabled });
        setSchedulerLoaded(s.enabled);
        setSchedulerEnabled(s.enabled);
        schedulerSaved = true;
      }
      // PUT 后端会同步写 config + 重启引擎使参数生效，前端不再单独 pause/resume。
      // config 写盘不可回滚：写盘成功但重启失败时后端返回 restart_ok=false（非报错），
      // 此时提示「已保存但需手动重启」而非「保存失败」，避免误导用户以为改动丢失。
      if (perceptionDirty) {
        const updated = await updatePerceptionConfig({
          video_short_edge: videoShortEdge,
          omni_fps: omniFps,
          window_size: windowSize,
          transmission_mode: transmissionMode,
        });
        setConfig(updated);
        if (updated.restart_ok === false) {
          toast(t("settings.restartFailed"), "warn");
        } else {
          toast(t("settings.applySuccess"), "ok");
        }
      }
      // 调度开关写盘当下并不生效（要等 agent 网关下次重启），与感知参数「即时生效」
      // 区分。独立于感知分支单发：仅改开关时是唯一 toast；与感知同改时在感知 toast
      // 之上再堆一条，补全开关的「延迟生效」措辞——否则双改会只走感知的
      // applySuccess，把开关也说成已即时生效（过度承诺）。
      if (schedulerSaved) {
        toast(t("settings.schedulerSaved"), "ok");
      }
      onClose();
    } catch {
      // 部分成功(开关已存、感知失败)与全败区分:前者 schedulerDirty 已随
      // schedulerLoaded 收敛为 false,重试只补发感知那半,故文案要如实说明。
      toast(
        schedulerSaved
          ? t("settings.partialSaveFailed")
          : t("settings.saveFailed"),
        "danger",
      );
    } finally {
      setBusy(false);
    }
  }

  function handleReset() {
    setVideoShortEdge(DEFAULTS.video_short_edge);
    setOmniFps(DEFAULTS.omni_fps);
    setWindowSize(DEFAULTS.window_size);
    // 仅在开关可配置时才回默认 ON；不可用（schedulerLoaded===null，置灰）时保持
    // 当前视觉，避免把置灰的开关拨到 ON 且 schedulerDirty 恒 false 无从写盘。
    if (schedulerAvailable) setSchedulerEnabled(true);
  }

  const dirty = perceptionDirty || schedulerDirty;

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
                <p className="text-caption text-text-tertiary">
                  {t("settings.omniFpsHint")}
                </p>
              </div>

              {/* 传输模式 */}
              <div className="space-y-2.5">
                <label className="text-body font-medium text-text-primary block">
                  {t("settings.transmissionMode", "传输模式")}
                </label>
                <div className="flex gap-2">
                  {TRANSMISSION_MODES.map((v) => (
                    <button
                      key={v}
                      type="button"
                      onClick={() => setTransmissionMode(v)}
                      className={`flex-1 py-2.5 rounded-xl text-body transition-colors ${
                        transmissionMode === v
                          ? "bg-brand-primary text-white shadow-sm"
                          : "bg-bg-primary border border-border text-text-primary hover:border-brand-primary"
                      }`}
                    >
                      {v === "video" ? t("settings.tmVideo", "视频") : t("settings.tmScreenshot", "截图")}
                    </button>
                  ))}
                </div>
                <p className="text-caption text-text-tertiary">
                  {t("settings.transmissionModeHint", "截图模式跳过 H.264 编码，降低 CPU/内存占用；音频独立发送")}
                </p>
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

              {/* 内置定时任务自动管理开关 */}
              <div className="space-y-2.5 pt-1 border-t border-border">
                <div className="flex items-center justify-between pt-5">
                  <label className="text-body font-medium text-text-primary">
                    {t("settings.autoSchedule")}
                  </label>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={schedulerEnabled}
                    disabled={!schedulerAvailable}
                    onClick={() => setSchedulerEnabled((v) => !v)}
                    className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                      schedulerEnabled ? "bg-brand-primary" : "bg-border"
                    } ${schedulerAvailable ? "" : "opacity-50 cursor-not-allowed"}`}
                  >
                    <span
                      className={`inline-block h-5 w-5 transform rounded-full bg-white shadow-sm transition-transform ${
                        schedulerEnabled ? "translate-x-[22px]" : "translate-x-0.5"
                      }`}
                    />
                  </button>
                </div>
                <p className="text-caption text-text-tertiary">
                  {schedulerAvailable
                    ? t("settings.autoScheduleHint")
                    : t("settings.autoScheduleUnavailable")}
                </p>
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
