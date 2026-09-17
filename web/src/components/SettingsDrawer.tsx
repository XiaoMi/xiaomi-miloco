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
import { getEdition } from "@/lib/edition";
import {
  buildPerceptionInputPayload,
  DEFAULT_IMAGE_LAST_FRAME_ONLY,
  DEFAULT_PERCEPTION_INPUT,
  normalizePerceptionInput,
  PERCEPTION_INPUT_MODES,
  type PerceptionInputMode,
} from "@/lib/perceptionInput";
import {
  buildPerceptionVerbosePayload,
  DEFAULT_PERCEPTION_VERBOSE,
  normalizePerceptionVerbose,
} from "@/lib/perceptionOutput";
import { toast } from "./Toast";

// PerceptionConfig 里 min_suggestion_urgency 声明为可选(老 backend 不返此字段);
// 但组件 state 需要确定值,单独拎一个具体类型的默认常量兜住:接口"可能没"、控件"永远有"。
const DEFAULT_MIN_URGENCY: MinSuggestionUrgency = "low";

// 与 backend 默认值对齐（video_short_edge 默认 768）：video_short_edge / omni_fps 见 settings.yaml 的
// perception.engine.input，window_size 见 perception.collect，smart_crop_enabled 见
// perception.engine.crop_enhance.user_enabled。min_suggestion_urgency 例外——它的默认值
// 不在 yaml 里，只在 settings.py::PerceptionSettings 的 pydantic Field（照 yaml 找会找不到）。
const DEFAULTS: PerceptionConfig = {
  video_short_edge: 768,
  omni_fps: 1,
  window_size: 4,
  smart_crop_enabled: true,
  min_suggestion_urgency: DEFAULT_MIN_URGENCY,
  global_system_prompt: "",
  // 感知输入默认「图片 + 只送末帧」（settings.yaml: rule_only_input=image /
  // last_frame_only=true）。常量与 payload 组装都在 @/lib/perceptionInput，
  // 单测引用同一份常量，不会各写一个字面量后悄悄漂移。
  perception_input: DEFAULT_PERCEPTION_INPUT,
  image_last_frame_only: DEFAULT_IMAGE_LAST_FRAME_ONLY,
  // 感知输出默认「只回命中规则 id」（settings.yaml: verbose=false）。判定理由的输出语言
  // 不在这里配——它跟随界面语言，由 i18n 侧同步给后端（见 lib/perceptionOutput）。
  perception_verbose: DEFAULT_PERCEPTION_VERBOSE,
};

// 全局感知系统提示词长度上限：与后端 PerceptionConfigBody 的 max_length 对齐，
// 前端先挡住以免用户写完才吃 422。
const GLOBAL_PROMPT_MAX = 8000;

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
  // slim（独立 App）不跑 omni 判定、不跑 agent 定时任务：帧率/紧急度/全局提示词/
  // 自动调度这四项在 slim 下拨了也不会有任何效果，直接不呈现，避免"看着能配、实则无效"。
  const slim = getEdition().slim;
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
  // 感知输入：图片（默认）/ 视频；图片模式下是否每窗只送最后一帧（默认否 = 多帧全发）。
  const [perceptionInput, setPerceptionInput] = useState<PerceptionInputMode>(
    DEFAULT_PERCEPTION_INPUT,
  );
  const [imageLastFrameOnly, setImageLastFrameOnly] = useState(
    DEFAULT_IMAGE_LAST_FRAME_ONLY,
  );
  // 感知模型详细判定输出（verbose）：默认关 = 只回命中规则 id 数组（省 token、更快）；
  // 开 = 逐条 hit + 不限字数 reason，排查漏报/误报时临时打开。
  const [perceptionVerbose, setPerceptionVerbose] = useState(
    DEFAULT_PERCEPTION_VERBOSE,
  );
  // 全局感知系统提示词：非空时后端追加到感知 system prompt 尾部（内置内容保留）。
  const [globalPrompt, setGlobalPrompt] = useState(
    DEFAULTS.global_system_prompt ?? "",
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
        // 老 backend 不返该字段 → 回退空串（= 不注入），与 backend 读取侧一致。
        setGlobalPrompt(c.global_system_prompt ?? "");
        // 老 backend 不返这两个字段 → 回退后端默认（图片 + 多帧），不误导成"远端就是这么配的"。
        setPerceptionInput(normalizePerceptionInput(c.perception_input));
        setImageLastFrameOnly(c.image_last_frame_only === true);
        // 老 backend 不返 verbose → 回退 false（= 默认只回命中 id）。
        setPerceptionVerbose(normalizePerceptionVerbose(c.perception_verbose));
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
      minUrgency !== (config.min_suggestion_urgency ?? DEFAULT_MIN_URGENCY) ||
      perceptionInput !== normalizePerceptionInput(config.perception_input) ||
      imageLastFrameOnly !== (config.image_last_frame_only === true) ||
      perceptionVerbose !== normalizePerceptionVerbose(config.perception_verbose) ||
      globalPrompt.trim() !== (config.global_system_prompt ?? "").trim());
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
          global_system_prompt: globalPrompt.trim(),
          ...buildPerceptionInputPayload(perceptionInput, imageLastFrameOnly),
          // 只提交抽屉里真正能改的字段。判定理由语言**不在这里改**（它跟随界面语言、由 i18n 侧
          // 同步）：PUT 是局部合并，硬回传一个读到的旧值反而会在"切过语言之后才点保存"时把语言
          // 写回旧值 —— 同一个坑的另一个入口，故干脆不传（后端保持原值）。
          ...buildPerceptionVerbosePayload(perceptionVerbose),
        });
        setConfig(updated);
        setSmartCrop(updated.smart_crop_enabled === true);
        setPerceptionInput(normalizePerceptionInput(updated.perception_input));
        setImageLastFrameOnly(updated.image_last_frame_only === true);
        setPerceptionVerbose(normalizePerceptionVerbose(updated.perception_verbose));
        // 回填后端规范化后的值（未来若后端做 trim/截断，前端随之收敛）。
        setGlobalPrompt(updated.global_system_prompt ?? "");
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
    setPerceptionInput(DEFAULT_PERCEPTION_INPUT);
    setImageLastFrameOnly(DEFAULT_IMAGE_LAST_FRAME_ONLY);
    setPerceptionVerbose(DEFAULT_PERCEPTION_VERBOSE);
    setGlobalPrompt(DEFAULTS.global_system_prompt ?? "");
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

              {/* 帧率（omni 采样率，slim 不跑 omni）*/}
              {!slim && (
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
              )}

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

              {/* 感知输入：图片/视频 + 图片是否只送末帧。只对 rule_only（slim 走这条）
                  生效，故非 slim 不呈现，避免"看着能配、实则无效"。两个开关都是热读，
                  保存后下个感知窗口生效，不需要重启引擎。 */}
              {slim && (
              <div className="space-y-2.5">
                <label className="text-body font-medium text-text-primary block">
                  {t("settings.perceptionInput")}
                </label>
                <div className="flex gap-2">
                  {PERCEPTION_INPUT_MODES.map((m) => (
                    <button
                      key={m}
                      type="button"
                      aria-pressed={perceptionInput === m}
                      onClick={() => setPerceptionInput(m)}
                      className={`flex-1 py-2.5 rounded-xl text-body transition-colors ${
                        perceptionInput === m
                          ? "bg-brand-primary text-white shadow-sm"
                          : "bg-bg-primary border border-border text-text-primary hover:border-brand-primary"
                      }`}
                    >
                      {t(
                        m === "image"
                          ? "settings.perceptionInputImage"
                          : "settings.perceptionInputVideo",
                      )}
                    </button>
                  ))}
                </div>
                <p className="text-caption text-text-tertiary leading-relaxed">
                  {t("settings.perceptionInputHint")}
                </p>

                {perceptionInput === "image" ? (
                  <div className="space-y-2 pt-1">
                    <div className="flex items-center justify-between">
                      <label className="text-body font-medium text-text-primary">
                        {t("settings.imageLastFrameOnly")}
                      </label>
                      <button
                        type="button"
                        role="switch"
                        aria-checked={imageLastFrameOnly}
                        aria-label={t("settings.imageLastFrameOnly")}
                        onClick={() => setImageLastFrameOnly((v) => !v)}
                        className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                          imageLastFrameOnly ? "bg-brand-primary" : "bg-border"
                        }`}
                      >
                        <span
                          className={`inline-block h-5 w-5 transform rounded-full bg-white shadow-sm transition-transform ${
                            imageLastFrameOnly ? "translate-x-[22px]" : "translate-x-0.5"
                          }`}
                        />
                      </button>
                    </div>
                    <p className="text-caption text-text-tertiary leading-relaxed">
                      {t("settings.imageLastFrameOnlyHint")}
                    </p>
                  </div>
                ) : (
                  <p className="text-caption text-text-tertiary leading-relaxed">
                    {t("settings.perceptionInputVideoNote")}
                  </p>
                )}

                {/* 详细判定输出（verbose）：与图片/视频、单帧/多帧都正交，故放在三元之外。
                    开 = 让模型逐条给出 hit 与不限字数的 reason，排查"该命中没命中 /
                    不该命中却命中"；关（默认）= 只回命中规则的 id 数组，判定更快。 */}
                <div className="space-y-2 border-t border-border pt-2.5">
                  <div className="flex items-center justify-between">
                    <label className="text-body font-medium text-text-primary">
                      {t("settings.perceptionVerbose")}
                    </label>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={perceptionVerbose}
                      aria-label={t("settings.perceptionVerbose")}
                      onClick={() => setPerceptionVerbose((v) => !v)}
                      className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                        perceptionVerbose ? "bg-brand-primary" : "bg-border"
                      }`}
                    >
                      <span
                        className={`inline-block h-5 w-5 transform rounded-full bg-white shadow-sm transition-transform ${
                          perceptionVerbose ? "translate-x-[22px]" : "translate-x-0.5"
                        }`}
                      />
                    </button>
                  </div>
                  <p className="text-caption text-text-tertiary leading-relaxed">
                    {t("settings.perceptionVerboseHint")}
                  </p>
                </div>
              </div>
              )}

              {/* 事件提醒 —— urgency 过滤(3-stop slider,与感知窗口视觉对齐)；omni 判定阈值，slim 不适用 */}
              {!slim && (
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
              )}

              {/* 全局感知系统提示词 —— 追加到感知 system prompt，对全部机位生效；
                  内置角色/总原则/schema 保留。逐场景判定细则在「场景联动」每条规则里配。
                  slim（独立 App）走 rule_only，但它同样被拼进 prompt、同样热生效，所以
                  这里保留（上方帧率/紧急度与下方定时任务是 omni 判定与 agent 调度参数，
                  slim 下拨了也没用，仍然隐藏）。 */}
              <div className="space-y-2.5">
                <div className="flex items-center justify-between">
                  <label className="text-body font-medium text-text-primary">
                    {t("settings.globalPrompt")}
                  </label>
                  <span className="text-caption text-text-tertiary num">
                    {globalPrompt.length}/{GLOBAL_PROMPT_MAX}
                  </span>
                </div>
                <textarea
                  value={globalPrompt}
                  onChange={(e) => setGlobalPrompt(e.target.value)}
                  rows={6}
                  maxLength={GLOBAL_PROMPT_MAX}
                  placeholder={t("settings.globalPromptPlaceholder")}
                  className="w-full rounded-xl bg-bg-primary border border-border px-3 py-2 text-body text-text-primary focus:outline-none focus:border-brand-primary resize-y"
                />
                <p className="text-caption text-text-tertiary leading-relaxed">
                  {t("settings.globalPromptHint")}
                </p>
              </div>

              {/* 内置定时任务自动管理开关（agent 定时任务，slim 不启动 ScheduleRunner）*/}
              {!slim && (
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
              )}

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
