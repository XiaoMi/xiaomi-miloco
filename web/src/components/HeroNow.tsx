/**
 * 「家里此刻」Hero 区（v3 Mi Console 视觉）
 *
 * 整面板视觉重量最高的卡片：家人 chips + 摄像头实时画面 +
 * 全开/全关批量切换 + inUse 单卡 toggle。
 */

import type {
  CameraSchedule,
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
import { useId, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "./Toast";
import { switchBlockedReasonKey } from "@/lib/cameraSwitch";
import {
  channelHasMic,
  feedDid as synthFeedDid,
  lensLabelKey,
} from "@/lib/cameraChannel";
import {
  isCrossMidnightWindow,
  mergeScheduleWindows,
  normalizeTimeValue,
  scheduleWindowsEqual,
  weekdaysEqual,
} from "@/lib/cameraSchedule";
import { IconClock, IconPlus, IconRefresh, IconTrash, IconX } from "@/lib/icons";

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

const CAMERA_SCHEDULE_WEEKDAYS = [0, 1, 2, 3, 4, 5, 6];

const HOUR_OPTIONS = Array.from({ length: 24 }, (_, i) =>
  String(i).padStart(2, "0"),
);
const MINUTE_OPTIONS = Array.from({ length: 60 }, (_, i) =>
  String(i).padStart(2, "0"),
);

function Time24Input({
  value,
  disabled,
  onChange,
}: {
  value: string;
  disabled?: boolean;
  onChange: (next: string) => void;
}) {
  const { t } = useTranslation();
  const normalized = normalizeTimeValue(value) || "00:00";
  const [hour = "00", minute = "00"] = normalized.split(":");
  return (
    <div
      className={`flex min-w-0 items-center gap-1 rounded-md border border-border bg-bg-primary px-2 py-1.5 ${
        disabled ? "opacity-50" : ""
      }`}
    >
      <select
        aria-label={t("hero.scheduleHour")}
        value={hour}
        disabled={disabled}
        onChange={(e) => onChange(`${e.currentTarget.value}:${minute}`)}
        className="min-w-0 flex-1 appearance-none bg-transparent text-body text-text-primary outline-none disabled:cursor-not-allowed"
      >
        {HOUR_OPTIONS.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
      <span className="text-text-tertiary">:</span>
      <select
        aria-label={t("hero.scheduleMinute")}
        value={minute}
        disabled={disabled}
        onChange={(e) => onChange(`${hour}:${e.currentTarget.value}`)}
        className="min-w-0 flex-1 appearance-none bg-transparent text-body text-text-primary outline-none disabled:cursor-not-allowed"
      >
        {MINUTE_OPTIONS.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </div>
  );
}

interface Props {
  persons: Person[];
  /** 米家全集（含被禁用 / 离线），用于渲染所有摄像头卡片 + Switch。多通道相机每条
   *  通道一条记录（did 相同、channel 区分），channel 直接带在每条上供播放 / 分行。 */
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
  /** 手动刷新未感知设备状态（force 刷新相机在线 / 镜头 + await 列表重拉落地）。 */
  onRefresh?: () => void | Promise<void>;
  /** 设置摄像头每日感知时间段（按物理 did）。 */
  onSetCameraSchedule: (
    did: string,
    schedule: CameraSchedule,
  ) => void | Promise<void>;
}

// 排序:已认识在前,未认识统一靠后
function sortPersons(ps: Person[]): Person[] {
  return [...ps].sort((a, b) => {
    if (a.faceEnrolled !== b.faceEnrolled) return a.faceEnrolled ? -1 : 1;
    return 0;
  });
}

// 把「一台相机的若干通道行」按物理 did 聚成一组(单摄一条、双摄两条),保持传入顺序
// (调用方已按 did、channel 排序)。方案 A:启停按整台走,故分组后每组只出一套开关,
// 画面 / 状态仍按通道逐条展示。每组即一台相机的若干通道行。
export function HeroNow({
  persons,
  scopeCameras,
  miotHasCamera,
  maxStreamCams,
  onPersonClick,
  onJumpUsage,
  onToggleCameras,
  onToggleCameraVoice,
  onRefresh,
  onSetCameraSchedule,
}: Props) {
  const { t } = useTranslation();
  const sorted = sortPersons(persons);
  // 上区 = miloco **当前真正在投喂视频** 的相机。判据用后端权威字段 `connected`
  // (= MiotService._connected_camera_dids() = 感知 camera_adapter.get_connected_devices()，
  // 即真正建连、在喂解码帧给感知的那几路)，而不是 `inUse`(只是 KV 里的"想启用"意图——
  // 启用了但 LAN 拉不起来时 inUse=true 却没真投喂)。再 **按 (did, channel) 稳定排序**:
  // toggle 某路时其余卡 key+DOM 位置不变，React 复用其 iframe，不会连带把其它路的 watch
  // 流断开重连。其余相机(未投喂:未启用 / 启用中未连上 / 超出上限)进下区「无流」横向列表。
  const { streamingCams, benchCams } = useMemo(() => {
    // 多通道相机两条记录 did 相同，需按 channel 二级排序 + 用 (did, channel) 复合键
    // 判定归属，否则同名两行会互相顶掉（同一 did 只留一条）。
    const key = (c: ScopeCamera) => `${c.did}|${c.channel}`;
    const byDidChannel = (a: ScopeCamera, b: ScopeCamera) => {
      if (a.did !== b.did) return a.did < b.did ? -1 : 1;
      return a.channel - b.channel;
    };
    const sorted = [...scopeCameras].sort(byDidChannel);
    // 不再前端截断:connected 集天然受后端 MAX_ENABLED_CAMERAS 约束(感知接入层按流路数
    // 截断、只连前 N 路；主动 enable 超限也被 toggle_camera 挡下)，展示集即真实投喂集。
    const streaming = sorted.filter((c) => c.connected);
    const sset = new Set(streaming.map(key));
    const bench = sorted.filter((c) => !sset.has(key(c)));
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
        onToggleCameras={onToggleCameras}
        onToggleCameraVoice={onToggleCameraVoice}
        onRefresh={onRefresh}
        onSetCameraSchedule={onSetCameraSchedule}
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
  onToggleCameras: (dids: string[], inUse: boolean) => void | Promise<void>;
  onToggleCameraVoice: (did: string, voiceInUse: boolean) => void | Promise<void>;
  onRefresh?: () => void | Promise<void>;
  onSetCameraSchedule: (
    did: string,
    schedule: CameraSchedule,
  ) => void | Promise<void>;
}

function CameraSection({
  scopeCameras,
  streamingCams,
  benchCams,
  maxStreamCams,
  miotHasCamera,
  onToggleCameras,
  onToggleCameraVoice,
  onRefresh,
  onSetCameraSchedule,
}: CameraSectionProps) {
  const { t } = useTranslation();
  // 手动刷新未感知设备状态:in-flight 期间转圈 + disable 防连点(force 刷新本身绕过 8s 节流)。
  const [refreshing, setRefreshing] = useState(false);
  const runRefresh = async () => {
    if (refreshing || !onRefresh) return;
    setRefreshing(true);
    try {
      await onRefresh();
    } finally {
      setRefreshing(false);
    }
  };
  const total = scopeCameras.length;
  // 出现多于一条记录的物理 did = 多通道相机；这些相机每卡/每行才拼镜头标签，好让同名
  // 同房间的两条彼此区分（单通道不显示，免噪声）。逻辑收在 @/lib/cameraChannel（可单测）。
  // 多通道判据用后端权威信号 channelCount>1（与 backend/CLI 同口径），不用「同 did 出现几行」代理。
  const isMulti = (c: ScopeCamera): boolean => c.channelCount > 1;
  // 每路显镜头名（ch0=移动画面 / ch1=固定画面；ch≥2 兜底「通道 N」）；单摄不显示。
  const channelLabelOf = (c: ScopeCamera): string | undefined => {
    if (!isMulti(c)) return undefined;
    const key = lensLabelKey(c.channel);
    return key ? t(key) : t("hero.channelLabel", { n: c.channel });
  };
  // 全拆:每路逐通道渲染、各控自己那路。投喂开关目标 = 该路合成 did(多摄 `did:ch{n}`、单摄
  // 裸 did);拾音是相机级(mic 只在球机/ch0)、按物理 did。
  const feedDidOf = (c: ScopeCamera): string =>
    synthFeedDid(c.did, c.channel, isMulti(c));
  const hasMic = (c: ScopeCamera): boolean => channelHasMic(c.channel);
  const activeCount = scopeCameras.filter((c) => c.inUse).length;
  const allOn = total > 0 && activeCount === total;
  const allOff = activeCount === 0;
  // 满额判断按 inUse(=活跃集:未拉黑 + 三态好 + 上限内)计数,与后端 toggle_camera 的
  // 上限校验同口径——后端也数「可用集」(离线/局域网不可达/镜头关的不占名额)。所以
  // 面板显示的名额 = 后端认的名额,不会出现「看着有位、点开启却被后端拒」。
  const atCapacity = activeCount >= maxStreamCams;
  // 「全开」只能开「可用且未投喂」的**通道**——不可用(云端离线/局域网不可达/该路镜头关)
  // 后端 toggle_camera 会整批拒绝。全拆按通道走：每路一个合成 did，各自独立(不去重物理 did)。
  const enableableDids = scopeCameras
    .filter((c) => !c.inUse && cameraAvailable(c))
    .map(feedDidOf);
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

  const [scheduleCam, setScheduleCam] = useState<ScopeCamera | null>(null);
  const [scheduleBusyDid, setScheduleBusyDid] = useState<string | null>(null);
  const saveSchedule = async (did: string, schedule: CameraSchedule) => {
    if (scheduleBusyDid) return;
    setScheduleBusyDid(did);
    try {
      await onSetCameraSchedule(did, schedule);
      setScheduleCam(null);
    } finally {
      setScheduleBusyDid(null);
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
                  scopeCameras.filter((c) => c.inUse).map(feedDidOf),
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
          {/* 上区:最多 4 路「带实时流」的相机,按物理 did 分组——每台一张卡(卡内每通道
              一个小窗),整台一套开关。卡 key=物理 did,稳定排序,toggle 某台时其余卡
              key+位置不变，React 复用其 iframe，不会连带把其它台的流断开重连。 */}
          {streamingCams.length > 0 ? (
            <div className="flex gap-3 overflow-x-auto snap-x snap-mandatory pb-2 -mx-1 px-1">
              {streamingCams.map((c) => {
                const feedDid = feedDidOf(c);
                return (
                  <CamCardWithToggle
                    key={feedDid}
                    cam={c}
                    channelLabel={channelLabelOf(c)}
                    // 拾音只在有 mic 的通道(球机/ch0)显示;枪机(ch1)永久无音频不给开关。
                    showVoice={hasMic(c)}
                    bulkBusy={bulkBusy || singleBusyDids.has(feedDid)}
                    onToggle={(v) => runSingle(feedDid, v)}
                    // 投喂开关 in-flight(按该路合成 did)时拾音也置灰,防交叠竞态。拾音是
                    // 相机级,按物理 did 跟踪。
                    voiceBusy={
                      voiceBusyDids.has(c.did) ||
                      bulkBusy ||
                      singleBusyDids.has(feedDid)
                    }
                    onToggleVoice={(v) => requestVoiceToggle(c.did, c.name, v)}
                    onSchedule={() => setScheduleCam(c)}
                  />
                );
              })}
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
              <div className="flex items-center justify-between gap-2 mb-2">
                <span className="text-caption text-text-tertiary">
                  {atCapacity
                    ? t("hero.benchTitleFull", { n: maxStreamCams })
                    : t("hero.benchTitle")}
                </span>
                {onRefresh && (
                  <button
                    type="button"
                    onClick={runRefresh}
                    // refreshing 覆盖整个手动刷新(onRefresh 里 await 到列表重拉落地),故只看它;
                    // 点其他开关触发的 reload 不置 refreshing,刷新图标不会被借用转圈。
                    disabled={refreshing}
                    aria-label={t("hero.refreshCamerasAria")}
                    title={t("hero.refreshCamerasTitle")}
                    className="shrink-0 text-text-tertiary hover:text-text-primary disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    <IconRefresh
                      width={15}
                      height={15}
                      className={refreshing ? "animate-spin" : ""}
                    />
                  </button>
                )}
              </div>
              <ul className="rounded-xl bg-bg-secondary border border-border divide-y divide-border overflow-hidden">
                {benchCams.map((c) => {
                  const feedDid = feedDidOf(c);
                  return (
                    <BenchCamItem
                      key={feedDid}
                      cam={c}
                      channelLabel={channelLabelOf(c)}
                      showVoice={hasMic(c)}
                      // 瞬态忙才原生禁用;语义不可开(离线 / 该路镜头关 / 局域网不可达 / 满额)走
                      // blockedReasonKey——置灰但可点,点击 toast、桌面悬停气泡说明原因。
                      // awake 用**该路**的 per-lens 值(全拆后每行一路,不再整台 OR)。
                      busy={bulkBusy || singleBusyDids.has(feedDid)}
                      blockedReasonKey={switchBlockedReasonKey(
                        {
                          cloudOnline: c.cloudOnline,
                          lanReachable: c.lanReachable,
                          awake: c.awake,
                        },
                        { inUse: c.inUse, atCapacity },
                      )}
                      onToggle={(v) => runSingle(feedDid, v)}
                      voiceBusy={
                        voiceBusyDids.has(c.did) ||
                        bulkBusy ||
                        singleBusyDids.has(feedDid)
                      }
                      onToggleVoice={(v) => requestVoiceToggle(c.did, c.name, v)}
                      onSchedule={() => setScheduleCam(c)}
                    />
                  );
                })}
              </ul>
            </div>
          )}
          {scheduleCam && (
            <CameraScheduleDialog
              cam={scheduleCam}
              busy={scheduleBusyDid === scheduleCam.did}
              onClose={() => setScheduleCam(null)}
              onSave={(schedule) => saveSchedule(scheduleCam.did, schedule)}
            />
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
  busy,
  blockedReasonKey,
  onToggle,
}: {
  inUse: boolean;
  name: string;
  /** 瞬态忙（bulk / single 操作进行中）：真禁、忽略点击、不提示。 */
  busy: boolean;
  /** 语义不可开（离线 / 镜头关 / 局域网不可达 / 满额，仅未启用时）：置灰但仍可点 → toast 理由；已启用为空。 */
  blockedReasonKey?: string;
  onToggle: (next: boolean) => void;
}) {
  const { t } = useTranslation();
  const blocked = !!blockedReasonKey;
  const dim = busy || blocked;
  // 桌面 hover 点缀:语义不可开时在鼠标入场处弹原因气泡,与点击 toast 互补——hover 即时看
  // 原因,点击给明确反馈 + 触屏兜底。fixed 定位锚定入场点(只 onMouseEnter 记一次、不跟随
  // 光标,省逐像素重渲染),不被下区 ul 的 overflow-hidden 裁掉;触屏无 hover,只走 toast。
  const [tip, setTip] = useState<{ x: number; y: number } | null>(null);
  // SR 用:稳定 id 关联「原因文本」与开关。aria-describedby 需常驻(见下 sr-only 文本),
  // 不依赖 tip——否则 SR 聚焦瞬间视觉气泡可能还没渲染、describedby 落空。
  const tipId = useId();
  return (
    <>
      <button
        type="button"
        role="switch"
        aria-checked={inUse}
        aria-disabled={dim}
        // block 态常驻指向 sr-only 原因文本,让屏幕阅读器聚焦即读出「为什么不可开」。
        aria-describedby={blocked ? tipId : undefined}
        aria-label={t(inUse ? "hero.toggleAriaInUse" : "hero.toggleAriaNotInUse", {
          name,
        })}
        title={
          blocked
            ? undefined
            : t(inUse ? "hero.toggleTitleInUse" : "hero.toggleTitleNotInUse")
        }
        disabled={busy}
        onClick={() => {
          if (busy) return;
          if (blocked) {
            toast(t(blockedReasonKey), "warn");
            return;
          }
          onToggle(!inUse);
        }}
        onMouseEnter={
          blocked ? (e) => setTip({ x: e.clientX, y: e.clientY }) : undefined
        }
        onMouseLeave={() => setTip(null)}
        // 键盘可达:block 态开关仍可 Tab 聚焦,focus 时也弹原因气泡(取按钮外接矩形顶边中点
        // 作锚点),与 hover 对齐——纯键盘用户 Tab 停上来即看到原因,不必等激活 toast 才知道。
        onFocus={
          blocked
            ? (e) => {
                const r = e.currentTarget.getBoundingClientRect();
                setTip({ x: r.left + r.width / 2, y: r.top });
              }
            : undefined
        }
        onBlur={() => setTip(null)}
        className={`relative inline-flex h-[14px] w-[26px] shrink-0 rounded-full transition-colors shadow-sm focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none ${
          dim ? "opacity-40 cursor-not-allowed" : ""
        } ${inUse ? "bg-brand-primary" : "bg-black/60"}`}
      >
        <span
          className={`absolute top-0.5 left-0.5 inline-block h-2.5 w-2.5 rounded-full bg-white shadow-sm transition-transform ${
            inUse ? "translate-x-[12px]" : "translate-x-0"
          }`}
        />
      </button>
      {/* SR 用:block 态常挂一份 sr-only 原因文本(视觉隐藏),承载 aria-describedby——常驻
          不依赖 tip,故屏幕阅读器聚焦瞬间就能读到,不受视觉气泡渲染时机影响。 */}
      {blocked && (
        <span id={tipId} className="sr-only">
          {t(blockedReasonKey)}
        </span>
      )}
      {/* 视觉气泡:hover / focus 弹,纯视觉——aria-hidden 避免与上面 sr-only 文本重复朗读。 */}
      {blocked && tip && (
        <div
          aria-hidden="true"
          className="fixed z-[90] w-56 max-w-[70vw] -translate-x-1/2 -translate-y-full rounded-lg border border-warning bg-warning-bg text-warning text-caption px-2.5 py-1.5 shadow-md pointer-events-none"
          style={{ left: tip.x, top: tip.y - 10 }}
        >
          {t(blockedReasonKey)}
        </div>
      )}
    </>
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

function ScheduleButton({
  active,
  name,
  onClick,
  variant,
}: {
  active: boolean;
  name: string;
  onClick: () => void;
  variant: "overlay" | "inline";
}) {
  const { t } = useTranslation();
  const classes =
    variant === "overlay"
      ? `inline-flex h-[26px] w-[26px] items-center justify-center rounded-md bg-black/55 text-white hover:bg-black/70 focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none ${
          active ? "text-brand-primary" : ""
        }`
      : `inline-flex h-[28px] w-[28px] items-center justify-center rounded-md border border-border bg-bg-primary text-text-tertiary hover:border-border-strong hover:text-text-primary focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none ${
          active ? "text-brand-primary border-brand-primary/40" : ""
        }`;
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={t("hero.scheduleAria", { name })}
      title={t("hero.scheduleTitle")}
      className={classes}
    >
      <IconClock className="h-4 w-4" />
    </button>
  );
}

function ScheduleBadge({
  tone,
  children,
}: {
  tone: "brand" | "warning";
  children: ReactNode;
}) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] leading-none ${
        tone === "brand"
          ? "bg-brand-primary/15 text-brand-primary"
          : "bg-warning-bg text-warning"
      }`}
    >
      {children}
    </span>
  );
}

interface CamCardProps {
  /** 一路通道(全拆后每路一张卡)。 */
  cam: ScopeCamera;
  /** 「通道 N」标签;单摄 undefined（不显示）。 */
  channelLabel?: string;
  /** 是否显示拾音开关——只有 mic 的通道(球机/ch0)给,枪机(ch1)永久无音频不给。 */
  showVoice: boolean;
  /** 父级 bulk 操作（全开/全关）正在进行——单卡 Switch 也得 disable 防交叠 PUT */
  bulkBusy: boolean;
  onToggle: (next: boolean) => void;
  onSchedule: () => void;
  /** 拾音开关置灰条件:拾音 PUT 或本路投喂 PUT in-flight（防两个 PUT 交叠竞态） */
  voiceBusy: boolean;
  onToggleVoice: (next: boolean) => void;
}

// 上区卡只渲染「正在投喂 miloco（connected）」的相机——必然是活流，无需蒙层。
// 全拆:每路一张卡(一个画面 + 该路自己的投喂开关);多摄名字/画面角标「通道 N」区分镜头,
// 拾音只在有 mic 的通道显示。房间贴名字(相机级)。
function CamCardWithToggle({
  cam,
  channelLabel,
  showVoice,
  bulkBusy,
  onToggle,
  onSchedule,
  voiceBusy,
  onToggleVoice,
}: CamCardProps) {
  const { t } = useTranslation();
  return (
    <div className="snap-start shrink-0 w-[min(280px,85vw)]">
      <div className="relative">
        <LivePlayerPlaceholder
          cameraName={cam.name}
          roomName={cam.roomName}
          cameraDid={cam.did}
          channel={cam.channel}
        />
        {/* 画面左上角标相机名(单/多摄一致都显示);多摄再后缀镜头标签(移动/固定画面)区分同台两路。 */}
        <span className="absolute top-2 left-2 max-w-[calc(100%-1rem)] truncate px-1.5 py-0.5 rounded-md bg-black/50 text-white text-caption pointer-events-none z-10">
          {channelLabel ? `${cam.name} · ${channelLabel}` : cam.name}
        </span>
        {/* 全拆后每路一个独立开关 → 定时 + 拾音 + 投喂。定时按物理机共享。
            拾音仅有 mic 的通道(球机/ch0)显示。 */}
        <div className="absolute top-2 right-2 flex items-center gap-1.5">
          <ScheduleButton
            active={cam.schedule.enabled}
            name={cam.name}
            onClick={onSchedule}
            variant="overlay"
          />
          {showVoice && (
            <VoiceSwitch
              on={cam.inUse && cam.voiceInUse}
              name={cam.name}
              disabled={!cam.inUse || voiceBusy}
              onToggle={onToggleVoice}
            />
          )}
          <CamSwitch
            inUse={cam.inUse}
            name={cam.name}
            busy={bulkBusy}
            onToggle={onToggle}
          />
        </div>
        {(cam.schedulePaused || cam.cappedOut) && (
          <div className="absolute bottom-2 left-2 flex flex-wrap gap-1.5">
            {cam.schedulePaused && (
              <ScheduleBadge tone="brand">
                {t("hero.schedulePaused")}
              </ScheduleBadge>
            )}
            {cam.cappedOut && (
              <ScheduleBadge tone="warning">
                {t("hero.scheduleCappedOut")}
              </ScheduleBadge>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** 下区横条行（日志页风格）：摄像头信息 + 拾音/投喂开关，无小窗。投喂 on → 升入上区。
 *  全拆:每路一行、各控自己那路。行内 名字(+通道标签+房间) + 该路三态灯 + 该路投喂开关;
 *  拾音只在有 mic 的通道显示。 */
function BenchCamItem({
  cam,
  channelLabel,
  showVoice,
  busy,
  blockedReasonKey,
  onToggle,
  voiceBusy,
  onToggleVoice,
  onSchedule,
}: {
  cam: ScopeCamera;
  channelLabel?: string;
  showVoice: boolean;
  busy: boolean;
  blockedReasonKey?: string;
  onToggle: (next: boolean) => void;
  voiceBusy: boolean;
  onToggleVoice: (next: boolean) => void;
  onSchedule: () => void;
}) {
  const { t } = useTranslation();
  const available = cameraAvailable(cam);
  return (
    <li className="px-4 py-3 flex items-center justify-between gap-3 hover:bg-bg-tertiary transition-colors">
      <div className="min-w-0">
        {/* 名字 + 通道标签 + 房间 badge 同一行;不可用(该路镜头关/离线/不可达)名字淡化,
            跟开关呼应——不可用就别让住户以为点一下能投喂。 */}
        <div className="flex items-center gap-1.5 min-w-0">
          <span
            className={`text-body truncate ${
              available ? "text-text-primary" : "text-text-tertiary"
            }`}
          >
            {cam.name}
          </span>
          {channelLabel && (
            <span className="shrink-0 text-caption text-text-tertiary">
              {channelLabel}
            </span>
          )}
          {cam.roomName && (
            <span className="shrink-0 text-caption text-text-tertiary border border-border rounded px-1.5 py-0.5 leading-none">
              {cam.roomName}
            </span>
          )}
        </div>
        {/* 该路自己的三态灯 + 定时/满额徽标。 */}
        <div className="flex items-center flex-wrap gap-x-2 gap-y-0.5 mt-0.5">
          <ChannelStateDots cam={cam} bare />
          {cam.schedulePaused && (
            <ScheduleBadge tone="brand">{t("hero.schedulePaused")}</ScheduleBadge>
          )}
          {cam.cappedOut && (
            <ScheduleBadge tone="warning">
              {t("hero.scheduleCappedOut")}
            </ScheduleBadge>
          )}
        </div>
      </div>
      {/* 定时 + 拾音 + 投喂。定时按物理机共享。 */}
      <div className="flex items-center gap-2 shrink-0">
        <ScheduleButton
          active={cam.schedule.enabled}
          name={cam.name}
          onClick={onSchedule}
          variant="inline"
        />
        {showVoice && (
          <VoiceSwitch
            on={cam.inUse && cam.voiceInUse}
            name={cam.name}
            disabled={!cam.inUse || voiceBusy}
            onToggle={onToggleVoice}
          />
        )}
        <CamSwitch
          inUse={cam.inUse}
          name={cam.name}
          busy={busy}
          blockedReasonKey={blockedReasonKey}
          onToggle={onToggle}
        />
      </div>
    </li>
  );
}

function CameraScheduleDialog({
  cam,
  busy,
  onClose,
  onSave,
}: {
  cam: ScopeCamera;
  busy: boolean;
  onClose: () => void;
  onSave: (schedule: CameraSchedule) => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const [enabled, setEnabled] = useState(cam.schedule.enabled);
  const [weekdays, setWeekdays] = useState(
    cam.schedule.weekdays.length > 0
      ? [...cam.schedule.weekdays]
      : [...CAMERA_SCHEDULE_WEEKDAYS],
  );
  const [windows, setWindows] = useState(
    cam.schedule.windows.length > 0
      ? cam.schedule.windows.map((w) => ({ ...w }))
      : [{ start: "08:00", end: "20:00" }],
  );
  const [error, setError] = useState<string | null>(null);

  const updateWindow = (
    index: number,
    key: "start" | "end",
    value: string,
  ) => {
    setWindows((items) =>
      items.map((item, i) =>
        i === index ? { ...item, [key]: normalizeTimeValue(value) } : item,
      ),
    );
  };
  const removeWindow = (index: number) => {
    setWindows((items) => items.filter((_, i) => i !== index));
  };
  const toggleWeekday = (weekday: number) => {
    setWeekdays((items) =>
      items.includes(weekday)
        ? items.filter((item) => item !== weekday)
        : [...items, weekday].sort((a, b) => a - b),
    );
  };
  const applyWeekdayPreset = (preset: readonly number[]) => {
    setWeekdays([...preset]);
  };
  const submit = async () => {
    if (enabled && weekdays.length === 0) {
      setError(t("hero.scheduleNeedWeekday"));
      return;
    }
    if (enabled && windows.length === 0) {
      setError(t("hero.scheduleNeedWindow"));
      return;
    }
    if (enabled && windows.some((w) => !w.start || !w.end)) {
      setError(t("hero.scheduleInvalid"));
      return;
    }
    const normalizedWindows = windows.map((window) => ({
      start: normalizeTimeValue(window.start),
      end: normalizeTimeValue(window.end),
    }));
    // 重叠/相邻区间自动合并，用户只需关心「哪些时间要感知」
    const mergedWindows = enabled
      ? mergeScheduleWindows(normalizedWindows)
      : normalizedWindows;
    if (enabled && mergedWindows.length === 0) {
      setError(t("hero.scheduleNeedWindow"));
      return;
    }
    if (!scheduleWindowsEqual(mergedWindows, normalizedWindows)) {
      setWindows(mergedWindows);
    }
    setError(null);
    await onSave({
      enabled: enabled && mergedWindows.length > 0,
      weekdays,
      windows: mergedWindows,
    });
  };

  const weekdayPresets = [
    {
      id: "everyday",
      days: CAMERA_SCHEDULE_WEEKDAYS,
      label: t("hero.schedulePresetEveryday"),
    },
    {
      id: "weekdays",
      days: [0, 1, 2, 3, 4],
      label: t("hero.schedulePresetWeekdays"),
    },
    {
      id: "weekend",
      days: [5, 6],
      label: t("hero.schedulePresetWeekend"),
    },
  ] as const;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 px-4">
      <div className="w-full max-w-md rounded-xl border border-border bg-bg-secondary shadow-xl">
        <div className="flex items-center justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h3 className="text-subtitle text-text-primary truncate">
              {t("hero.scheduleDialogTitle")}
            </h3>
            <div className="text-caption text-text-tertiary truncate">
              {cam.name}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("common.close")}
            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-text-tertiary hover:bg-bg-tertiary hover:text-text-primary focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none"
          >
            <IconX className="h-4 w-4" />
          </button>
        </div>
        <div className="space-y-4 px-5 py-4">
          <label className="flex items-center justify-between gap-4">
            <span className="text-body text-text-primary">
              {t("hero.scheduleEnabled")}
            </span>
            <button
              type="button"
              role="switch"
              aria-checked={enabled}
              onClick={() => setEnabled((v) => !v)}
              className={`relative inline-flex h-[18px] w-[34px] shrink-0 rounded-full transition-colors focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none ${
                enabled ? "bg-brand-primary" : "bg-black/35"
              }`}
            >
              <span
                className={`absolute top-0.5 left-0.5 inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform ${
                  enabled ? "translate-x-4" : "translate-x-0"
                }`}
              />
            </button>
          </label>
          <div className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <div className="text-caption text-text-tertiary">
                {t("hero.scheduleWeekdays")}
              </div>
              <div className="flex flex-wrap justify-end gap-1">
                {weekdayPresets.map((preset) => {
                  const selected = weekdaysEqual(weekdays, preset.days);
                  return (
                    <button
                      key={preset.id}
                      type="button"
                      aria-pressed={selected}
                      disabled={!enabled}
                      onClick={() => applyWeekdayPreset(preset.days)}
                      className={`rounded-md border px-2 py-0.5 text-caption transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                        selected
                          ? "border-brand-primary bg-brand-primary/10 text-brand-primary"
                          : "border-border bg-bg-primary text-text-secondary hover:border-border-strong hover:text-text-primary"
                      }`}
                    >
                      {preset.label}
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="grid grid-cols-7 gap-1.5">
              {CAMERA_SCHEDULE_WEEKDAYS.map((weekday) => {
                const selected = weekdays.includes(weekday);
                return (
                  <button
                    key={weekday}
                    type="button"
                    aria-pressed={selected}
                    onClick={() => toggleWeekday(weekday)}
                    disabled={!enabled}
                    className={`min-w-0 rounded-md border px-0 py-1.5 text-caption transition-colors focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-40 ${
                      selected
                        ? "border-brand-primary bg-brand-primary text-white"
                        : "border-border bg-bg-primary text-text-secondary hover:border-border-strong hover:text-text-primary"
                    }`}
                  >
                    {t(`hero.scheduleWeekdayShort.${weekday}`)}
                  </button>
                );
              })}
            </div>
          </div>
          <div className="space-y-2">
            {windows.map((window, index) => {
              const crossMidnight = isCrossMidnightWindow(window);
              return (
                <div
                  key={`${index}-${window.start}-${window.end}`}
                  className="space-y-1.5 rounded-md border border-border bg-bg-primary/40 p-2"
                >
                  <div className="grid grid-cols-[1fr_auto_1fr_auto] items-end gap-2">
                    <div className="min-w-0 space-y-1">
                      {crossMidnight && (
                        <span className="inline-flex rounded bg-brand-primary/10 px-1.5 py-0.5 text-[11px] text-brand-primary">
                          {t("hero.scheduleSameDay")}
                        </span>
                      )}
                      <Time24Input
                        value={window.start}
                        disabled={!enabled}
                        onChange={(next) => updateWindow(index, "start", next)}
                      />
                    </div>
                    <span className="pb-2 text-text-tertiary">-</span>
                    <div className="min-w-0 space-y-1">
                      {crossMidnight && (
                        <span className="inline-flex rounded bg-brand-primary/10 px-1.5 py-0.5 text-[11px] text-brand-primary">
                          {t("hero.scheduleNextDay")}
                        </span>
                      )}
                      <Time24Input
                        value={window.end}
                        disabled={!enabled}
                        onChange={(next) => updateWindow(index, "end", next)}
                      />
                    </div>
                    <button
                      type="button"
                      onClick={() => removeWindow(index)}
                      disabled={!enabled || windows.length <= 1}
                      aria-label={t("hero.scheduleRemoveWindow")}
                      title={t("hero.scheduleRemoveWindow")}
                      className="mb-0.5 inline-flex h-9 w-9 items-center justify-center rounded-md border border-border text-text-tertiary hover:border-border-strong hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      <IconTrash className="h-4 w-4" />
                    </button>
                  </div>
                  {crossMidnight && (
                    <div className="text-caption text-text-tertiary">
                      {t("hero.scheduleCrossMidnightHint")}
                    </div>
                  )}
                </div>
              );
            })}
            <button
              type="button"
              onClick={() =>
                setWindows((items) => [
                  ...items,
                  { start: "08:00", end: "20:00" },
                ])
              }
              disabled={!enabled}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-caption text-text-secondary hover:border-border-strong hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-40"
            >
              <IconPlus className="h-4 w-4" />
              {t("hero.scheduleAddWindow")}
            </button>
          </div>
          {cam.schedulePaused && (
            <div className="text-caption text-brand-primary">
              {t("hero.schedulePausedDetail", {
                time: formatScheduleTime(cam.nextScheduleChangeAt),
              })}
            </div>
          )}
          {enabled && !cam.inUse && (
            <div className="text-caption text-text-tertiary">
              {t("hero.scheduleMasterOffHint")}
            </div>
          )}
          {error && <div className="text-caption text-warning">{error}</div>}
        </div>
        <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-4">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border px-3 py-2 text-body text-text-secondary hover:border-border-strong hover:text-text-primary"
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={busy}
            className="rounded-md bg-brand-primary px-3 py-2 text-body text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? t("common.saving") : t("common.save")}
          </button>
        </div>
      </div>
    </div>
  );
}

function formatScheduleTime(value: string | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 一路通道的三个并列可用性指标:云端在线 / 局域网可达 / 镜头开关。各自独立好坏,
 *  住户能一眼看出卡在哪一环。下区单摄行与多摄子行复用。 */
function ChannelStateDots({
  cam,
  bare = false,
}: {
  cam: ScopeCamera;
  bare?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div
      className={
        bare
          ? "text-caption flex items-center flex-wrap gap-x-2 gap-y-0.5"
          : "text-caption flex items-center flex-wrap gap-x-2 gap-y-0.5 mt-0.5"
      }
    >
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
    </div>
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
