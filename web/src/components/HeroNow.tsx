/**
 * 「家里此刻」Hero 区（v3 Mi Console 视觉）
 *
 * 整面板视觉重量最高的卡片：家人 chips + 摄像头实时画面 +
 * 全开/全关批量切换 + inUse 单卡 toggle。
 */

import type {
  PerceptionCamera,
  Person,
  ScopeCamera,
  UsageStats,
} from "@/lib/types";
import { cameraAvailable } from "@/lib/types";
import { PersonChip } from "./PersonChip";
import { LivePlayerPlaceholder } from "./LivePlayerPlaceholder";
import { getUsageStats } from "@/api";
import { useAsync } from "@/hooks/useAsync";
import { humanTokens } from "@/lib/formatTokens";
import { useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

// 「关声音」确认弹窗的「不再提醒」持久化标记（与 web:theme / web:lang 同命名空间）。
// 复位说明：清除站点数据 / localStorage 即恢复弹窗；本分支不做设置项 UI——将来若加
// 隐私设置面板，一句 localStorage.removeItem(VOICE_ON_CONFIRMED_KEY) 即可重置。
const VOICE_ON_CONFIRMED_KEY = "web:voiceOnConfirmed";

function isVoiceOnConfirmed(): boolean {
  try {
    return localStorage.getItem(VOICE_ON_CONFIRMED_KEY) === "1";
  } catch {
    return false; // localStorage 不可用(隐私模式/测试桩)→ 每次仍确认,无害
  }
}

function setVoiceOnConfirmed(): void {
  try {
    localStorage.setItem(VOICE_ON_CONFIRMED_KEY, "1");
  } catch {
    /* 写不了就算了：本会话每次仍弹确认,不影响功能 */
  }
}

interface Props {
  persons: Person[];
  /** perception 当前订阅的画面（含 channel，用于真播放）；scope 是子集映射的字典源 */
  cameras: PerceptionCamera[];
  /** 米家全集（含被禁用 / 离线），用于渲染所有摄像头卡片 + Switch */
  scopeCameras: ScopeCamera[];
  /** miot 上是否有 camera 类设备——区分两种空态 */
  miotHasCamera: boolean;
  /** 最多投喂给 miloco 的摄像头数(后端 MAX_ENABLED_CAMERAS，经 /api/miot/status 下发)。
   *  上区展示数受 connected 自然约束，此值只用于下区「启用」按钮的满额置灰判断。 */
  maxStreamCams: number;
  /** undefined → chip 渲染成 div(无 hover/点击反馈),概览页用 */
  onPersonClick?: (p: Person) => void;
  /** 点击"今日用量"小卡片跳到用量 tab。 */
  onJumpUsage?: () => void;
  /** 切换摄像头启用（PUT /api/miot/scope/cameras）；批量传 dids */
  onToggleCameras: (dids: string[], inUse: boolean) => void | Promise<void>;
  /** 切换单台摄像头拾音（PUT /api/miot/scope/cameras/voice）。关闭 = 该相机声音完全
   *  不被处理（mic-off：不转写、不上云）。从属于感知开关：仅当该相机 inUse=true 时
   *  可设，感知关时前端置灰。 */
  onToggleCameraVoice: (did: string, voiceInUse: boolean) => void | Promise<void>;
}

// 排序:已认识在前,未认识统一靠后
function sortPersons(ps: Person[]): Person[] {
  return [...ps].sort((a, b) => {
    if (a.faceEnrolled !== b.faceEnrolled) return a.faceEnrolled ? -1 : 1;
    return 0;
  });
}

export function HeroNow({
  persons,
  cameras,
  scopeCameras,
  miotHasCamera,
  maxStreamCams,
  onPersonClick,
  onJumpUsage,
  onToggleCameras,
  onToggleCameraVoice,
}: Props) {
  const { t } = useTranslation();
  const sorted = sortPersons(persons);
  // scope 是主列表（含禁用/离线/未接入）；cameras 仅用作 channel 字典。
  // useMemo 让 Map 引用稳定—— Map 每次 render 重建会让传到 CameraSection 的 prop
  // 引用变更,父级状态变（如 todayUsage 异步到达）触发的 re-render 会冲掉子组件
  // memo 优化机会。
  const channelByDid = useMemo(
    () => new Map(cameras.map((c) => [c.did, c.channel])),
    [cameras],
  );
  // 上区 = miloco **当前真正在投喂视频** 的相机。判据用后端权威字段 `connected`
  // (= MiotService._connected_camera_dids() = 感知 camera_adapter.get_connected_devices()，
  // 即真正建连、在喂解码帧给感知的那几路)，而不是 `inUse`(只是 KV 里的"想启用"意图——
  // 启用了但 LAN 拉不起来时 inUse=true 却没真投喂)。再 **按 did 稳定排序**:toggle 某路时
  // 其余卡 key+DOM 位置不变，React 复用其 iframe，不会连带把其它路的 watch 流断开重连。
  // 其余相机(未投喂:未启用 / 启用中未连上 / 超出上限)进下区「无流」横向列表。
  const { streamingCams, benchCams } = useMemo(() => {
    const byDid = (a: ScopeCamera, b: ScopeCamera) =>
      a.did < b.did ? -1 : a.did > b.did ? 1 : 0;
    const sorted = [...scopeCameras].sort(byDid);
    // 不再前端截断:connected 集天然受后端 MAX_ENABLED_CAMERAS 约束(感知接入层按 did
    // 升序截断到上限、只连前 N 路；主动 enable 超限也被 toggle_camera 挡下)，展示集即真实投喂集。
    const streaming = sorted.filter((c) => c.connected);
    const sset = new Set(streaming.map((c) => c.did));
    const bench = sorted.filter((c) => !sset.has(c.did));
    return { streamingCams: streaming, benchCams: bench };
  }, [scopeCameras]);
  // 今日 token 用量小入口（omni 计费）
  const todayUsage = useAsync<UsageStats>(
    () => getUsageStats("today"),
    [],
    { errorLabel: "" },
  );

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6 anim-in"
      aria-labelledby="hero-now-title"
    >
      <div className="flex items-baseline justify-between gap-3 mb-4 flex-wrap">
        <h2
          id="hero-now-title"
          className="text-title text-text-primary inline-flex items-baseline gap-2"
        >
          {t("hero.title")}
          <span className="text-caption-mono text-text-tertiary font-normal">
            now
          </span>
        </h2>
        {todayUsage.data && (
          <button
            type="button"
            onClick={onJumpUsage}
            disabled={!onJumpUsage}
            aria-label={t("hero.usageAriaLabel")}
            title={t("hero.usageTitle")}
            className="text-caption inline-flex items-baseline gap-1.5 text-text-secondary hover:text-brand-primary transition-colors disabled:cursor-default disabled:hover:text-text-secondary"
          >
            <span>{t("hero.usageToday")}</span>
            <span className="num text-text-primary">
              {humanTokens(todayUsage.data.total_tokens)}
            </span>
            <span className="text-text-tertiary">{t("hero.usageTokens")}</span>
            {onJumpUsage && (
              <span className="text-text-tertiary" aria-hidden>→</span>
            )}
          </button>
        )}
      </div>

      {/* 家人 */}
      <SectionLabel>{t("hero.familyLabel")}</SectionLabel>
      {sorted.length === 0 ? (
        <div className="text-body text-text-secondary mb-5">
          {t("hero.familyEmpty")}
        </div>
      ) : (
        <div className="flex flex-wrap gap-2 mb-5">
          {sorted.map((p) => (
            <PersonChip
              key={p.id}
              person={p}
              onClick={onPersonClick ? () => onPersonClick(p) : undefined}
            />
          ))}
        </div>
      )}

      {/* 摄像头 */}
      <CameraSection
        scopeCameras={scopeCameras}
        streamingCams={streamingCams}
        benchCams={benchCams}
        maxStreamCams={maxStreamCams}
        miotHasCamera={miotHasCamera}
        channelByDid={channelByDid}
        onToggleCameras={onToggleCameras}
        onToggleCameraVoice={onToggleCameraVoice}
      />
    </section>
  );
}

interface CameraSectionProps {
  scopeCameras: ScopeCamera[];
  /** 上区:带实时流的相机(inUse=true，≤4，按 did 稳定排序) */
  streamingCams: ScopeCamera[];
  /** 下区:无流横向列出的相机(未启用 / 超出上限) */
  benchCams: ScopeCamera[];
  /** 最多投喂数(后端 MAX_ENABLED_CAMERAS)，用于满额置灰下区「启用」 */
  maxStreamCams: number;
  miotHasCamera: boolean;
  channelByDid: Map<string, number>;
  onToggleCameras: (dids: string[], inUse: boolean) => void | Promise<void>;
  onToggleCameraVoice: (did: string, voiceInUse: boolean) => void | Promise<void>;
}

function CameraSection({
  scopeCameras,
  streamingCams,
  benchCams,
  maxStreamCams,
  miotHasCamera,
  channelByDid,
  onToggleCameras,
  onToggleCameraVoice,
}: CameraSectionProps) {
  const { t } = useTranslation();
  const total = scopeCameras.length;
  const activeCount = scopeCameras.filter((c) => c.inUse).length;
  const allOn = total > 0 && activeCount === total;
  const allOff = activeCount === 0;
  // 满额判断按 inUse 计数(与后端 toggle_camera 上限校验同口径):已启用的相机即便
  // 掉线仍保留 inUse、占名额(允许态不被强制改),要腾名额得显式关掉它。
  const atCapacity = activeCount >= maxStreamCams;
  // 「全开」只能开「可用且未投喂」的——不可用相机(云端离线/局域网不可达/镜头关)后端
  // toggle_camera 会整批拒绝,若把它们塞进批量 enable,会连带可用的一起失败。
  // 与下区单台开关「不可用不可开」同口径(cameraAvailable)。
  const enableableDids = scopeCameras
    .filter((c) => !c.inUse && cameraAvailable(c))
    .map((c) => c.did);
  // bulkBusy 锁防"全开/全关"连点;singleBusyDids 跟踪单卡 in-flight,让住户切单卡 A
  // 时只 disable A 卡,B/C/D 仍可点。bulk 操作进行时仍 disable 所有(防交叠)。
  const [bulkBusy, setBulkBusy] = useState(false);
  const [singleBusyDids, setSingleBusyDids] = useState<Set<string>>(new Set());
  // 拾音开关独立 in-flight 集：拾音 PUT 走独立端点,与投喂 PUT 互不阻塞,分开跟踪。
  const [voiceBusyDids, setVoiceBusyDids] = useState<Set<string>>(new Set());
  const runBulk = async (dids: string[], inUse: boolean) => {
    if (bulkBusy) return;
    setBulkBusy(true);
    try {
      await onToggleCameras(dids, inUse);
    } finally {
      setBulkBusy(false);
    }
  };
  const runSingle = async (did: string, inUse: boolean) => {
    if (bulkBusy || singleBusyDids.has(did)) return;
    setSingleBusyDids((s) => new Set(s).add(did));
    try {
      await onToggleCameras([did], inUse);
    } finally {
      setSingleBusyDids((s) => {
        const n = new Set(s);
        n.delete(did);
        return n;
      });
    }
  };
  const runSingleVoice = async (did: string, voiceInUse: boolean) => {
    if (voiceBusyDids.has(did)) return;
    setVoiceBusyDids((s) => new Set(s).add(did));
    try {
      await onToggleCameraVoice(did, voiceInUse);
    } finally {
      setVoiceBusyDids((s) => {
        const n = new Set(s);
        n.delete(did);
        return n;
      });
    }
  };
  // 声音默认关（opt-in）：开启方向先弹一次知情提示，讲清可能的问题与适用场景；关闭
  // 方向无害（只是停止处理声音），直接执行。待确认的相机存这里。用户勾「不再提醒」并
  // 确认后，落 localStorage 标记，之后开声音直接执行、不再弹（批量开多台安静机位时不啰嗦）。
  const [pendingVoiceOn, setPendingVoiceOn] = useState<{
    did: string;
    name: string;
  } | null>(null);
  const [dontRemind, setDontRemind] = useState(false);
  const requestVoiceToggle = (did: string, name: string, next: boolean) => {
    if (voiceBusyDids.has(did)) return;
    if (!next) {
      void runSingleVoice(did, false); // 关闭声音无需确认（无害）
    } else if (isVoiceOnConfirmed()) {
      void runSingleVoice(did, true); // 已选「不再提醒」→ 直接开
    } else {
      setDontRemind(false); // 每次开框默认不勾
      setPendingVoiceOn({ did, name }); // 开启 → 知情提示
    }
  };

  return (
    <>
      <div className="flex items-baseline justify-between flex-wrap gap-2 mb-2">
        <div className="flex items-baseline gap-2">
          <SectionLabel>{t("hero.liveLabel")}</SectionLabel>
        </div>
        {total > 0 && (
          <div className="text-caption flex items-center gap-2 text-text-tertiary">
            <span className="num">
              {t("hero.perceivingCount", { n: activeCount })}
            </span>
            <button
              type="button"
              onClick={() => runBulk(enableableDids, true)}
              // 满额(activeCount≥上限)时置灰,跟下区单台启用开关口径一致——否则
              // >上限 的家庭点"全开"必被后端 toggle_camera 的上限校验整批拒绝。
              // 无「在线且未投喂」的相机时也置灰(全离线 / 已全开),避免空批 / 整批被拒。
              disabled={
                allOn || bulkBusy || atCapacity || enableableDids.length === 0
              }
              className="px-2 py-0.5 rounded-md bg-bg-primary border border-border hover:border-border-strong hover:text-text-primary disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {t("hero.allOn")}
            </button>
            <button
              type="button"
              onClick={() =>
                runBulk(
                  scopeCameras.filter((c) => c.inUse).map((c) => c.did),
                  false,
                )
              }
              disabled={allOff || bulkBusy}
              className="px-2 py-0.5 rounded-md bg-bg-primary border border-border hover:border-border-strong hover:text-text-primary disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {t("hero.allOff")}
            </button>
          </div>
        )}
      </div>
      {total === 0 ? (
        <div className="text-body rounded-lg bg-bg-primary border border-dashed border-border-strong text-text-secondary py-8 px-5 text-center">
          {miotHasCamera ? (
            <>
              <div className="text-warning mb-1">
                {t("hero.cameraOfflineTitle")}
              </div>
              <div>
                {t("hero.cameraOfflineHint")}
              </div>
            </>
          ) : (
            <>{t("hero.cameraEmpty")}</>
          )}
        </div>
      ) : (
        <>
          {/* 上区:最多 4 路「带实时流」。streamingCams 按 did 稳定排序——toggle 某路时
              其余卡 key+位置不变，React 复用其 iframe，不会连带把其它路的流断开重连。 */}
          {streamingCams.length > 0 ? (
            <div className="flex gap-3 overflow-x-auto snap-x snap-mandatory pb-2 -mx-1 px-1">
              {streamingCams.map((c) => (
                <CamCardWithToggle
                  key={c.did}
                  cam={c}
                  channel={channelByDid.get(c.did)}
                  bulkBusy={bulkBusy || singleBusyDids.has(c.did)}
                  onToggle={(v) => runSingle(c.did, v)}
                  // 相机开关 in-flight 时拾音开关也置灰:关相机的 PUT 落库后拾音 PUT 会被
                  // 后端「感知已关闭」拒掉,别让住户在窗口期点出个报错 toast。
                  voiceBusy={
                    voiceBusyDids.has(c.did) ||
                    bulkBusy ||
                    singleBusyDids.has(c.did)
                  }
                  onToggleVoice={(v) => requestVoiceToggle(c.did, c.name, v)}
                />
              ))}
            </div>
          ) : (
            <div className="text-body rounded-lg bg-bg-primary border border-dashed border-border-strong text-text-secondary py-6 px-5 text-center">
              {t("hero.noStreaming")}
            </div>
          )}
          {/* 下区:未投喂给 miloco 的相机(未启用 / 超出上限)。不拉流、不展示小窗，
              改成日志页风格的横条行:每行 摄像头信息 + 一个开关，开关直接控制是否投喂。 */}
          {benchCams.length > 0 && (
            <div className="mt-4">
              <SectionLabel>
                {atCapacity
                  ? t("hero.benchTitleFull", { n: maxStreamCams })
                  : t("hero.benchTitle")}
              </SectionLabel>
              <ul className="rounded-xl bg-bg-secondary border border-border divide-y divide-border overflow-hidden">
                {benchCams.map((c) => (
                  <BenchCamItem
                    key={c.did}
                    cam={c}
                    // 不可用 + 未投喂 → 禁用(开不了);不可用 + 已投喂 → 仍可点(允许关闭)。
                    // 满额时也只挡「开启未启用的」,已启用的随时可关。即:仅当
                    // 「当前未启用 且 (不可用 或 已满额)」时禁用,其余可点。
                    disabled={
                      bulkBusy ||
                      singleBusyDids.has(c.did) ||
                      (!c.inUse && (!cameraAvailable(c) || atCapacity))
                    }
                    onToggle={(v) => runSingle(c.did, v)}
                    // 同上区卡:相机开关 in-flight 时拾音开关一并置灰,防交叠竞态。
                    voiceBusy={
                      voiceBusyDids.has(c.did) ||
                      bulkBusy ||
                      singleBusyDids.has(c.did)
                    }
                    onToggleVoice={(v) => requestVoiceToggle(c.did, c.name, v)}
                  />
                ))}
              </ul>
            </div>
          )}
        </>
      )}

      {/* 开声音知情提示：opt-in。讲清可能的问题 + 适用/不适用场景。复用居中弹窗形态。 */}
      {pendingVoiceOn && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40"
          onClick={
            voiceBusyDids.has(pendingVoiceOn.did)
              ? undefined
              : () => setPendingVoiceOn(null)
          }
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="voice-on-title"
            className="w-[90%] max-w-sm bg-bg-secondary border border-border rounded-2xl shadow-lg p-6 anim-in"
            onClick={(e) => e.stopPropagation()}
          >
            <h2
              id="voice-on-title"
              className="text-title font-semibold text-text-primary mb-2"
            >
              {t("hero.voiceOnConfirmTitle", { name: pendingVoiceOn.name })}
            </h2>
            <p className="text-body text-text-secondary mb-3">
              {t("hero.voiceOnConfirmIntro")}
            </p>
            {/* 可能的问题 + 适用/不适用场景：三行图标标记，一眼可辨。 */}
            <ul className="flex flex-col gap-2 mb-5 text-body">
              <li className="flex gap-2">
                <span className="text-warning shrink-0" aria-hidden="true">⚠</span>
                <span className="text-text-secondary">
                  {t("hero.voiceOnConfirmRisk")}
                </span>
              </li>
              <li className="flex gap-2">
                <span className="text-success shrink-0" aria-hidden="true">✓</span>
                <span className="text-text-secondary">
                  {t("hero.voiceOnConfirmRecommend")}
                </span>
              </li>
              <li className="flex gap-2">
                <span className="text-error shrink-0" aria-hidden="true">✕</span>
                <span className="text-text-secondary">
                  {t("hero.voiceOnConfirmAvoid")}
                </span>
              </li>
            </ul>
            {/* 不再提醒：勾选并确认后落 localStorage,之后开声音直接执行、不再弹框。 */}
            <label className="flex items-center gap-2 mb-5 text-body text-text-secondary cursor-pointer select-none">
              <input
                type="checkbox"
                checked={dontRemind}
                onChange={(e) => setDontRemind(e.target.checked)}
                className="h-4 w-4 rounded border-border accent-brand-primary cursor-pointer"
              />
              {t("hero.voiceOnConfirmDontRemind")}
            </label>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setPendingVoiceOn(null)}
                disabled={voiceBusyDids.has(pendingVoiceOn.did)}
                className="text-body px-4 py-2 rounded-lg bg-bg-primary border border-border text-text-primary hover:border-border-strong disabled:opacity-60"
              >
                {t("hero.voiceOnConfirmCancel")}
              </button>
              <button
                type="button"
                onClick={() => {
                  const { did } = pendingVoiceOn;
                  if (dontRemind) setVoiceOnConfirmed();
                  setPendingVoiceOn(null);
                  void runSingleVoice(did, true);
                }}
                disabled={voiceBusyDids.has(pendingVoiceOn.did)}
                className="text-body px-4 py-2 rounded-lg font-semibold bg-brand-primary text-white hover:opacity-90 disabled:opacity-60"
              >
                {t("hero.voiceOnConfirmOk")}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/** 投喂开关。上区卡(浮在画面上)与下区横条行复用。on=把这一路投给 miloco。 */
function CamSwitch({
  inUse,
  name,
  disabled,
  onToggle,
}: {
  inUse: boolean;
  name: string;
  disabled: boolean;
  onToggle: (next: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      role="switch"
      aria-checked={inUse}
      aria-label={t(inUse ? "hero.toggleAriaInUse" : "hero.toggleAriaNotInUse", {
        name,
      })}
      title={inUse ? t("hero.toggleTitleInUse") : t("hero.toggleTitleNotInUse")}
      disabled={disabled}
      onClick={() => onToggle(!inUse)}
      className={`relative inline-flex h-[14px] w-[26px] shrink-0 rounded-full transition-colors shadow-sm focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none disabled:opacity-40 disabled:cursor-not-allowed ${
        inUse ? "bg-brand-primary" : "bg-black/60"
      }`}
    >
      <span
        className={`absolute top-0.5 left-0.5 inline-block h-2.5 w-2.5 rounded-full bg-white shadow-sm transition-transform ${
          inUse ? "translate-x-[12px]" : "translate-x-0"
        }`}
      />
    </button>
  );
}

/** 拾音开关（mic-off：关闭后此摄像头的声音完全不被处理——不监听、不转写、不上云）。
 *  从属于感知开关：相机感知关(inUse=false)时置灰、显示为「关」
 *  (生效态 = inUse && voiceInUse)；感知开时反映并编辑存储偏好 voiceInUse。
 *  与投喂开关(CamSwitch)并排,靠麦克风图标 + 文字标签区分,免得两个开关混淆。 */
function VoiceSwitch({
  on,
  name,
  disabled,
  onToggle,
}: {
  on: boolean;
  name: string;
  disabled: boolean;
  onToggle: (next: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={t(on ? "hero.voiceAriaOn" : "hero.voiceAriaOff", { name })}
      title={
        disabled
          ? t("hero.voiceTitleDisabled")
          : on
            ? t("hero.voiceTitleOn")
            : t("hero.voiceTitleOff")
      }
      disabled={disabled}
      onClick={() => onToggle(!on)}
      className={`inline-flex items-center gap-1 h-[16px] pl-1 pr-1.5 rounded-full text-[10px] leading-none shadow-sm transition-colors focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none disabled:opacity-40 disabled:cursor-not-allowed ${
        on ? "bg-brand-primary text-white" : "bg-black/60 text-white/85"
      }`}
    >
      <MicIcon muted={!on} />
      <span>{t("hero.voiceLabel")}</span>
    </button>
  );
}

/** 小麦克风图标；muted=true 画一道斜杠,表示该相机拾音关闭（声音不被处理）。 */
function MicIcon({ muted }: { muted: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-3 w-3 shrink-0"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0" />
      <path d="M12 18v3" />
      {muted && <path d="M4 4l16 16" />}
    </svg>
  );
}

interface CamCardProps {
  cam: ScopeCamera;
  /** PerceptionCamera 提供的真 channel；undefined = 还没拉到 / 多家庭场景无映射 */
  channel: number | undefined;
  /** 父级 bulk 操作（全开/全关）正在进行——单卡 Switch 也得 disable 防交叠 PUT */
  bulkBusy: boolean;
  onToggle: (next: boolean) => void;
  /** 拾音开关置灰条件:拾音 PUT 或相机开关 PUT 本卡 in-flight（防两个 PUT 交叠竞态） */
  voiceBusy: boolean;
  onToggleVoice: (next: boolean) => void;
}

// 上区卡只渲染「正在投喂 miloco（connected）」的相机——必然是活流，无需蒙层。
function CamCardWithToggle({
  cam,
  channel,
  bulkBusy,
  onToggle,
  voiceBusy,
  onToggleVoice,
}: CamCardProps) {
  return (
    <div className="snap-start shrink-0 w-[min(280px,85vw)]">
      <div className="relative">
        <LivePlayerPlaceholder
          cameraName={cam.name}
          roomName={cam.roomName}
          cameraDid={cam.did}
          channel={channel ?? 0}
        />
        {/* 拾音 + 投喂两个开关并排浮在画面右上;connected 卡必然 inUse=true,拾音可编辑。 */}
        <div className="absolute top-2 right-2 flex items-center gap-1.5">
          <VoiceSwitch
            on={cam.inUse && cam.voiceInUse}
            name={cam.name}
            disabled={!cam.inUse || voiceBusy}
            onToggle={onToggleVoice}
          />
          <CamSwitch
            inUse={cam.inUse}
            name={cam.name}
            disabled={bulkBusy}
            onToggle={onToggle}
          />
        </div>
      </div>
    </div>
  );
}

/** 下区横条行（日志页风格）：摄像头信息 + 拾音/投喂开关，无小窗。投喂 on → 升入上区。 */
function BenchCamItem({
  cam,
  disabled,
  onToggle,
  voiceBusy,
  onToggleVoice,
}: {
  cam: ScopeCamera;
  disabled: boolean;
  onToggle: (next: boolean) => void;
  voiceBusy: boolean;
  onToggleVoice: (next: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <li className="px-4 py-3 flex items-center justify-between gap-3 hover:bg-bg-tertiary transition-colors">
      <div className="min-w-0">
        {/* 不可用相机名字淡化,跟开关禁用呼应——不可用就别让住户以为点一下能投喂。 */}
        <div
          className={`text-body truncate ${
            cameraAvailable(cam) ? "text-text-primary" : "text-text-tertiary"
          }`}
        >
          {cam.name}
        </div>
        {/* 三个并列的可用性指标:云端在线 / 局域网可达 / 镜头开关。各自独立好坏,
            住户能一眼看出到底卡在哪一环,不再一把揉成"离线"。 */}
        <div className="text-caption flex items-center flex-wrap gap-x-2 gap-y-0.5 mt-0.5">
          <StateDot
            ok={cam.cloudOnline}
            label={
              cam.cloudOnline
                ? t("hero.stateCloudOnline")
                : t("hero.stateCloudOffline")
            }
          />
          <StateDot
            ok={cam.lanReachable}
            label={
              cam.lanReachable ? t("hero.stateLanOk") : t("hero.stateLanOffline")
            }
          />
          <StateDot
            ok={cam.awake === null ? "unknown" : cam.awake}
            label={
              cam.awake === false
                ? t("hero.stateSleeping")
                : cam.awake === null
                  ? t("hero.stateAwakeUnknown")
                  : t("hero.stateAwake")
            }
          />
          {cam.roomName && (
            <span className="text-text-tertiary">· {cam.roomName}</span>
          )}
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        {/* 拾音开关从属于感知:相机未启用(inUse=false)时置灰、显示为关。 */}
        <VoiceSwitch
          on={cam.inUse && cam.voiceInUse}
          name={cam.name}
          disabled={!cam.inUse || voiceBusy}
          onToggle={onToggleVoice}
        />
        <CamSwitch
          inUse={cam.inUse}
          name={cam.name}
          disabled={disabled}
          onToggle={onToggle}
        />
      </div>
    </li>
  );
}

/** 单个可用性指标:一个圆点 + 文字。ok=true 绿点/常规色,false 橙点/警示色,
 *  "unknown"(镜头开关读不到)灰点/淡化。三个并列即"云端在线 | 局域网可达 | 镜头开启"。 */
function StateDot({ ok, label }: { ok: boolean | "unknown"; label: string }) {
  const dot =
    ok === true
      ? "bg-success"
      : ok === "unknown"
        ? "bg-text-tertiary"
        : "bg-warning";
  const text =
    ok === true
      ? "text-text-secondary"
      : ok === "unknown"
        ? "text-text-tertiary"
        : "text-warning";
  return (
    <span className={`inline-flex items-center gap-1 ${text}`}>
      <span className={`inline-block h-1.5 w-1.5 rounded-full ${dot}`} />
      {label}
    </span>
  );
}

/** 卡片内的小节标签——caption 字号 + tertiary 色,
 *  HeroNow 内复用。 */
function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <div className="text-caption text-text-tertiary mb-2">
      {children}
    </div>
  );
}
