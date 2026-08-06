import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getPerceptionConfig,
  getSchedulerConfig,
  updatePerceptionConfig,
  updateSchedulerConfig,
  type MinSuggestionUrgency,
  type PerceptionConfig,
} from "@/api";
import { useEscClose } from "@/hooks/useEscClose";
import { toast } from "./Toast";

// PerceptionConfig 里 min_suggestion_urgency 声明为可选(老 backend 不返此字段);
// 但组件 state 需要确定值,单独拎一个具体类型的默认常量兜住:接口"可能没"、控件"永远有"。
const DEFAULT_MIN_URGENCY: MinSuggestionUrgency = "low";

// 与 backend 默认值对齐：video_short_edge / omni_fps 见 settings.yaml 的
// perception.engine.input，window_size 见 perception.collect，smart_crop_enabled 见
// perception.engine.crop_enhance.user_enabled。min_suggestion_urgency 例外——它的默认值
// 不在 yaml 里，只在 settings.py::PerceptionSettings 的 pydantic Field（照 yaml 找会找不到）。
const DEFAULTS: PerceptionConfig = {
  video_short_edge: 512,
  omni_fps: 1,
  window_size: 4,
  smart_crop_enabled: true,
  min_suggestion_urgency: DEFAULT_MIN_URGENCY,
};

const SHORT_EDGE_OPTIONS = [360, 512, 768, 1080] as const;
const FPS_OPTIONS = [1, 2, 3] as const;
const WINDOW_MIN = 2;
const WINDOW_MAX = 10;
// slider 顺序即 URGENCY_RANK,index == pydantic Literal 顺序 → 双向 O(1) 映射。
const URGENCY_LEVELS: readonly MinSuggestionUrgency[] = ["low", "medium", "high"] as const;

interface Props {
  open: boolean;
  onClose: () => void;
}

export function SettingsDrawer({ open, onClose }: Props) {
  const { t } = useTranslation();
  const [config, setConfig] = useState<PerceptionConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const [videoShortEdge, setVideoShortEdge] = useState(DEFAULTS.video_short_edge);
  const [omniFps, setOmniFps] = useState(DEFAULTS.omni_fps);
  const [windowSize, setWindowSize] = useState(DEFAULTS.window_size);
  // Smart Crop 用户开关。与分辨率档正交（各自独立 dirty / 各自独立生效），
  // 不是第五个分辨率档 —— crop 视频的短边本身也按所选档等比跟随。
  const [smartCrop, setSmartCrop] = useState(DEFAULTS.smart_crop_enabled === true);
  const [minUrgency, setMinUrgency] = useState<MinSuggestionUrgency>(
    DEFAULT_MIN_URGENCY,
  );

  // 内置定时任务自动管理开关（scheduler.enabled）。缺省 true = 自动管理。
  const [schedulerLoaded, setSchedulerLoaded] = useState<boolean | null>(null);
  const [schedulerEnabled, setSchedulerEnabled] = useState(true);

  useEscClose(open, onClose);

  useEffect(() => {
    if (!open) {
      setConfig(null);
      setLoading(true);
      return;
    }
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
        setSmartCrop(c.smart_crop_enabled === true);
        // 老 backend 若不返 min_suggestion_urgency 时回退到"不过滤"默认,与 backend 的
        // Literal default 对齐,不误导用户以为拿到了远端值。
        setMinUrgency(c.min_suggestion_urgency ?? DEFAULT_MIN_URGENCY);
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

  // 发版级开关没放开时开关不可用（同 schedulerAvailable 的降级套路）：拨动不写盘，
  // 故置灰 + 换 hint，避免呈现「看着能动、实则后端不裁」的控件。
  // 老后端不返 smart_crop_available → undefined → 同样置灰。
  const smartCropAvailable = config?.smart_crop_available === true;
  const perceptionDirty =
    config != null &&
    (videoShortEdge !== config.video_short_edge ||
      omniFps !== config.omni_fps ||
      windowSize !== config.window_size ||
      // 不可用时恒 false：置灰的开关不该产出待保存改动
      (smartCropAvailable && smartCrop !== (config.smart_crop_enabled === true)) ||
      minUrgency !== (config.min_suggestion_urgency ?? DEFAULT_MIN_URGENCY));
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
          // 只在发版级开关放开时才提交,不可用时不往后端写一个用户按不动的值
          ...(smartCropAvailable ? { smart_crop_enabled: smartCrop } : {}),
          min_suggestion_urgency: minUrgency,
        });
        setConfig(updated);
        setSmartCrop(updated.smart_crop_enabled === true);
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
    // 同 scheduler：不可用（发版级开关未放开，置灰）时不动视觉，否则会拨出一个恒不 dirty 的值
    if (smartCropAvailable) setSmartCrop(DEFAULTS.smart_crop_enabled === true);
    setMinUrgency(DEFAULT_MIN_URGENCY);
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
            <div className="space-y-7 animate-pulse">
              <div className="space-y-2.5">
                <div className="h-4 w-16 bg-border rounded" />
                <div className="flex gap-2">
                  {Array.from({ length: 4 }, (_, i) => (
                    <div key={i} className="flex-1 h-11 bg-border rounded-xl" />
                  ))}
                </div>
              </div>
              <div className="space-y-2.5">
                <div className="h-4 w-12 bg-border rounded" />
                <div className="flex gap-2">
                  {Array.from({ length: 3 }, (_, i) => (
                    <div key={i} className="flex-1 h-11 bg-border rounded-xl" />
                  ))}
                </div>
              </div>
              <div className="space-y-2.5">
                <div className="h-4 w-20 bg-border rounded" />
                <div className="h-2 bg-border rounded-full" />
              </div>
              <div className="space-y-2.5">
                <div className="h-4 w-20 bg-border rounded" />
                <div className="h-6 w-11 bg-border rounded-full" />
              </div>
            </div>
          ) : !config ? (
            <div className="text-caption text-text-tertiary text-center py-8">
              {t("settings.loadFailed")}
            </div>
          ) : (
            <>
              {/* 分辨率 */}
              <div className="space-y-2.5">
                {/* 这两项只管云端通路。不写清楚的话会被读成"对本地 GPU 也生效",
                    而两条通路的成本结构是相反的(云端每帧每像素都要 token 钱,
                    本地的成本由 codec 的 token 预算封顶)。 */}
                <p className="text-caption text-text-tertiary mb-2">
                  {t("settings.cloudOnlyHint")}
                </p>
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

              {/* 智能裁切增强（Smart Crop）——独立开关，不是第五个分辨率档：
                  分辨率决定「多清晰」，裁切决定「看哪一块」，两者叠加生效
                  （crop 视频短边按上面所选档等比跟随）。 */}
              <div className="space-y-2.5">
                <div className="flex items-center justify-between">
                  <label className="text-body font-medium text-text-primary">
                    {t("settings.smartCrop")}
                  </label>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={smartCrop}
                    aria-label={t("settings.smartCrop")}
                    disabled={!smartCropAvailable}
                    onClick={() => setSmartCrop((v) => !v)}
                    className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                      smartCrop ? "bg-brand-primary" : "bg-border"
                    } ${smartCropAvailable ? "" : "opacity-50 cursor-not-allowed"}`}
                  >
                    <span
                      className={`inline-block h-5 w-5 transform rounded-full bg-white shadow-sm transition-transform ${
                        smartCrop ? "translate-x-[22px]" : "translate-x-0.5"
                      }`}
                    />
                  </button>
                </div>
                <p className="text-caption text-text-tertiary">
                  {smartCropAvailable
                    ? t("settings.smartCropHint")
                    : t("settings.smartCropUnavailable")}
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

              {/* 事件提醒 —— urgency 过滤(3-stop slider,与感知窗口视觉对齐) */}
              <div className="space-y-2.5">
                <div className="flex items-center justify-between">
                  <label className="text-body font-medium text-text-primary">
                    {t("settings.minUrgency")}
                  </label>
                  <span className="text-body text-text-primary font-semibold">
                    {t(`settings.minUrgencyStatus.${minUrgency}`)}
                  </span>
                </div>
                {(() => {
                  const idx = URGENCY_LEVELS.indexOf(minUrgency);
                  const pct = (idx / (URGENCY_LEVELS.length - 1)) * 100;
                  return (
                    <input
                      type="range"
                      min={0}
                      max={URGENCY_LEVELS.length - 1}
                      step={1}
                      value={idx}
                      onChange={(e) =>
                        setMinUrgency(URGENCY_LEVELS[Number(e.target.value)])
                      }
                      className="settings-slider w-full"
                      style={{
                        background: `linear-gradient(to right, var(--color-brand-primary, #ff6900) ${pct}%, var(--color-border, #e5e5e5) ${pct}%)`,
                      }}
                    />
                  );
                })()}
                <div className="flex justify-between text-caption text-text-tertiary">
                  {URGENCY_LEVELS.map((v) => (
                    <span key={v}>{t(`settings.minUrgencyTick.${v}`)}</span>
                  ))}
                </div>
                <p className="text-caption text-text-tertiary">
                  {t("settings.minUrgencyHint")}
                </p>
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
