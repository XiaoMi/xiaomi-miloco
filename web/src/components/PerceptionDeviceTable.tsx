import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ScopeCamera } from "@/lib/types";
import { cameraAvailable } from "@/lib/types";
import { refreshCameraOnline, toggleScopeCamera, perceiveQuery } from "@/api";
import { toast } from "./Toast";
import { IconRefresh } from "@/lib/icons";
import { sortCamerasByDid, onlineCameras as onlineCamerasFn } from "./PerceptionDeviceTable.helpers";

interface Props {
  cameras: ScopeCamera[];
  /** 后端 MAX_ENABLED_CAMERAS。status 未到/出错时兜底 4，与后端默认一致。 */
  maxEnabledCameras: number;
  onChanged: () => void;
}

// 「不再提醒」持久化标记。开 audio 弹过确认 + 勾了不再弹，之后直接走、不再弹框。
const AUDIO_ON_CONFIRMED_KEY = "web:audioOnConfirmed";

function isAudioOnConfirmed(): boolean {
  try { return localStorage.getItem(AUDIO_ON_CONFIRMED_KEY) === "1"; }
  catch { return false; }
}

function setAudioOnConfirmed(): void {
  try { localStorage.setItem(AUDIO_ON_CONFIRMED_KEY, "1"); }
  catch { /* 写不了就算了 */ }
}

export function PerceptionDeviceTable({ cameras, maxEnabledCameras, onChanged }: Props) {
  const { t } = useTranslation();
  const [singleBusy, setSingleBusy] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [testingDid, setTestingDid] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<{
    did: string; name: string; video: string; audio: string;
    hasVideoErr?: boolean; hasAudioErr?: boolean;
  } | null>(null);
  // 「开音频」确认弹窗的状态机 —— 关方向无害直接走，开方向先知情提示。
  const [pendingAudioOn, setPendingAudioOn] = useState<{ name: string; run: () => void } | null>(null);
  const [dontRemind, setDontRemind] = useState(false);

  // 全拆后 scope/cameras 按通道返多行（同 did 不同 channel）。
  // Per-modality 开关是整台粒度——按物理 did 去重，一行一台相机。
  const deduped = useMemo(() => {
    const seen = new Set<string>();
    return cameras.filter((c) => {
      if (seen.has(c.did)) return false;
      seen.add(c.did);
      return true;
    });
  }, [cameras]);
  const sorted = useMemo(() => sortCamerasByDid(deduped), [deduped]);
  const online = useMemo(() => onlineCamerasFn(deduped), [deduped]);

  const activeCount = deduped.filter((c) => c.videoEnabled || c.audioEnabled).length;
  const atCapacity = activeCount >= maxEnabledCameras;

  const runTest = async (c: ScopeCamera) => {
    if (testingDid) return;
    setTestingDid(c.did);
    setTestResult(null);
    // 全拆后活跃感知源按合成通道 did；多通道需展开，单摄裸 did 原样
    const sources = c.channelCount > 1
      ? Array.from({ length: c.channelCount }, (_, i) => `${c.did}:ch${i}`)
      : [c.did];
    let video = ""; let audio = "";
    let videoErr = ""; let audioErr = "";
    if (c.videoEnabled) {
      try { video = await perceiveQuery(sources, "画面里有什么。"); }
      catch (e) { videoErr = e instanceof Error ? e.message : String(e); }
    }
    if (c.audioEnabled) {
      try { audio = await perceiveQuery(sources, "有什么声音。"); }
      catch (e) { audioErr = e instanceof Error ? e.message : String(e); }
    }
    setTestResult({ did: c.did, name: c.name,
      video: video || (c.videoEnabled ? "" : t("hero.table.off")),
      audio: audio || (c.audioEnabled ? "" : t("hero.table.off")),
      hasVideoErr: !!videoErr, hasAudioErr: !!audioErr });
    setTestingDid(null);
  };

  // 单台改一个 modality。audio 开方向走确认弹窗；audio 关、video 双向都直接走。
  const requestSingle = (did: string, kind: "video" | "audio", next: boolean) => {
    if (bulkBusy || singleBusy.has(did)) return;
    const cam = cameras.find((c) => c.did === did);
    if (!cam) return;
    if (kind === "audio" && next && !isAudioOnConfirmed()) {
      setDontRemind(false);
      setPendingAudioOn({ name: cam.name, run: () => { void runSingle(did, "audio", true); } });
      return;
    }
    void runSingle(did, kind, next);
  };

  const runSingle = async (did: string, kind: "video" | "audio", next: boolean) => {
    setSingleBusy((s) => new Set(s).add(did));
    try {
      await toggleScopeCamera([{
        did,
        ...(kind === "video" ? { videoEnabled: next } : { audioEnabled: next }),
      }]);
      onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("common.switchFailed"), "warn");
    } finally {
      setSingleBusy((s) => { const n = new Set(s); n.delete(did); return n; });
    }
  };

  // 总开关 = 整机 video+audio 同方向。next=true 时若开了 audio 仍走 opt-in 弹窗，
  // 由 pendingAudioOn 路径处理；这里只处理 next=false（关方向）和不开 audio 的情况。
  const requestMaster = (did: string, next: boolean) => {
    const cam = cameras.find((c) => c.did === did);
    if (!cam) return;
    if (next && cam.voiceInUse === false && !isAudioOnConfirmed()) {
      setDontRemind(false);
      setPendingAudioOn({ name: cam.name, run: () => { void runMaster(did, true); } });
      return;
    }
    void runMaster(did, next);
  };

  const runMaster = async (did: string, next: boolean) => {
    if (bulkBusy || singleBusy.has(did)) return;
    setSingleBusy((s) => new Set(s).add(did));
    try {
      await toggleScopeCamera([{
        did, videoEnabled: next, audioEnabled: next, inUse: next,
      }]);
      onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("common.switchFailed"), "warn");
    } finally {
      setSingleBusy((s) => { const n = new Set(s); n.delete(did); return n; });
    }
  };

  const executeBulk = async (enable: boolean) => {
    setBulkBusy(true);
    try {
      await toggleScopeCamera(online.map((c) => ({
        did: c.did, videoEnabled: enable, audioEnabled: enable, inUse: enable,
      })));
      onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("common.switchFailed"), "warn");
    } finally {
      setBulkBusy(false);
    }
  };

  // 全部启用 / 全部停用 —— 跟后端 MAX_ENABLED_CAMERAS 同步置灰，否则 >4 路必被整批拒。
  const runBulk = async (enable: boolean) => {
    if (bulkBusy || online.length === 0) return;
    if (enable && atCapacity) {
      toast(t("hero.table.atCapacityHint", { n: maxEnabledCameras }), "warn");
      return;
    }
    if (enable && !isAudioOnConfirmed() && online.some((c) => !c.voiceInUse)) {
      setDontRemind(false);
      setPendingAudioOn({ name: t("hero.bulkAllOn"), run: () => { void executeBulk(true); } });
      return;
    }
    await executeBulk(enable);
  };

  // 手动刷新未感知设备状态 —— 绕过 list_cameras_with_state 的 8s 节流，立刻拿到云端最新在线。
  const runRefresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      await refreshCameraOnline(undefined, true).catch(() => {});
      onChanged();
    } finally {
      setRefreshing(false);
    }
  };

  const allOn = online.length > 0 && online.every((c) => c.videoEnabled && c.audioEnabled);
  const allOff = online.length > 0 && online.every((c) => !c.videoEnabled && !c.audioEnabled);

  // 关掉最后一路时弹 toast —— 防止用户困惑「我只关了一个 modality，整机怎么都关了」。
  // 副作用: 关 modality 时 inUse 会跟着 false，让卡片 CamSwitch 也跟着关。
  // 这里用 useEffect 在 activeCount 跨零时触发一次性提示。
  const wasActive = usePrevious(activeCount);
  useEffect(() => {
    if (wasActive !== undefined && wasActive > 0 && activeCount === 0) {
      toast(t("hero.modalitiesLastOffToast"), "info");
    }
  }, [activeCount, wasActive, t]);

  return (
    <section className="mt-4 rounded-xl bg-bg-secondary border border-border shadow-sm anim-in"
             aria-labelledby="perception-table-title">
      <div className="flex items-baseline justify-between px-5 pt-4 pb-3 flex-wrap gap-2">
        <div className="flex items-baseline gap-2">
          <h2 id="perception-table-title" className="text-title text-text-primary">
            {/* 动态标题：满额 / 默认 —— 跟 main 「miloco 未感知设备」对齐。 */}
            {atCapacity
              ? t("hero.table.titleAtCapacity", { n: maxEnabledCameras })
              : t("hero.table.title")}
          </h2>
          <button type="button" onClick={runRefresh} disabled={refreshing}
            aria-label={t("hero.table.refreshAria")}
            title={t("hero.table.refreshTitle")}
            className="text-text-tertiary hover:text-text-primary disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
            <IconRefresh width={15} height={15} className={refreshing ? "animate-spin" : ""} />
          </button>
        </div>
        {online.length > 0 && (
          <div className="flex flex-wrap items-center gap-2">
            <button type="button"
              // 满额时置灰：跟后端 toggle_camera 的上限校验同口径，否则必被整批拒。
              disabled={bulkBusy || allOn || atCapacity}
              onClick={() => runBulk(true)}
              className="text-caption px-3 py-1 rounded border border-border hover:border-border-strong hover:text-text-primary disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
              {t("hero.bulkAllOn")}
            </button>
            <button type="button" disabled={bulkBusy || allOff}
              onClick={() => runBulk(false)}
              className="text-caption px-3 py-1 rounded border border-border hover:border-border-strong hover:text-text-primary disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
              {t("hero.bulkAllOff")}
            </button>
          </div>
        )}
      </div>

      {cameras.length === 0 ? (
        <div className="text-body text-text-secondary py-10 px-5 text-center">{t("hero.table.empty")}</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-body">
            <thead>
              <tr className="text-caption text-text-tertiary border-b border-border">
                <th className="text-left font-normal px-5 py-2">{t("hero.table.headerDevice")}</th>
                <th className="text-center font-normal px-3 py-2">{t("hero.table.headerVideo")}</th>
                <th className="text-center font-normal px-3 py-2">{t("hero.table.headerAudio")}</th>
                <th className="text-right font-normal px-5 py-2">{t("hero.table.headerActions")}</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((c) => {
                const offline = !cameraAvailable(c);
                const busy = bulkBusy || singleBusy.has(c.did);
                const togCls = (on: boolean) =>
                  `relative inline-flex h-[14px] w-[26px] shrink-0 rounded-full transition-colors shadow-sm ${
                    on ? "bg-brand-primary" : "bg-black/60"
                  } ${(offline||busy) ? "opacity-40 cursor-not-allowed" : "cursor-pointer"}`;
                return (
                  <tr key={c.did} className={`border-b border-border last:border-b-0 ${offline ? "opacity-50" : ""}`}>
                    <td className="px-5 py-3">
                      <div className="flex items-baseline gap-2">
                        <span className="text-text-primary truncate">{c.name}</span>
                        {c.roomName && <span className="text-caption text-text-tertiary truncate">· {c.roomName}</span>}
                      </div>
                      {offline && <div className="text-caption text-warning mt-0.5">{t("hero.table.offlineHint")}</div>}
                    </td>
                    <td className="px-3 py-3 text-center">
                      <button type="button" role="switch" aria-checked={c.videoEnabled}
                        disabled={offline||busy} onClick={() => requestSingle(c.did, "video", !c.videoEnabled)}
                        className={togCls(c.videoEnabled)}>
                        <span className={`absolute top-0.5 left-0.5 inline-block h-2.5 w-2.5 rounded-full bg-white shadow-sm transition-transform ${
                          c.videoEnabled ? "translate-x-[12px]" : "translate-x-0"}`} />
                      </button>
                    </td>
                    <td className="px-3 py-3 text-center">
                      <button type="button" role="switch" aria-checked={c.audioEnabled}
                        disabled={offline||busy} onClick={() => requestSingle(c.did, "audio", !c.audioEnabled)}
                        className={togCls(c.audioEnabled)}>
                        <span className={`absolute top-0.5 left-0.5 inline-block h-2.5 w-2.5 rounded-full bg-white shadow-sm transition-transform ${
                          c.audioEnabled ? "translate-x-[12px]" : "translate-x-0"}`} />
                      </button>
                    </td>
                    <td className="px-5 py-3 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <button type="button" role="switch"
                          aria-checked={c.videoEnabled && c.audioEnabled}
                          disabled={offline||busy}
                          onClick={() => requestMaster(c.did, !(c.videoEnabled && c.audioEnabled))}
                          className={`relative inline-flex h-[14px] w-[26px] shrink-0 rounded-full transition-colors shadow-sm ${
                            (c.videoEnabled && c.audioEnabled) ? "bg-brand-primary" : "bg-black/60"
                          } ${(offline||busy) ? "opacity-40 cursor-not-allowed" : "cursor-pointer"}`}>
                          <span className={`absolute top-0.5 left-0.5 inline-block h-2.5 w-2.5 rounded-full bg-white shadow-sm transition-transform ${
                            (c.videoEnabled && c.audioEnabled) ? "translate-x-[12px]" : "translate-x-0"}`} />
                        </button>
                        {(!c.videoEnabled && !c.audioEnabled) ? (
                          <span title={t("hero.table.testDisabled")}
                            className="text-caption px-2 py-1 rounded border border-border text-text-tertiary opacity-40 cursor-not-allowed">
                            {t("hero.table.testBtn")}
                          </span>
                        ) : (
                          <button type="button" disabled={offline || testingDid === c.did}
                            onClick={() => runTest(c)}
                            className="text-caption px-2 py-1 rounded border border-border hover:border-border-strong disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
                            {testingDid === c.did ? (
                              <span className="inline-block h-3 w-3 border-2 border-text-tertiary border-t-transparent rounded-full animate-spin" />
                            ) : (
                              t("hero.table.testBtn")
                            )}
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {testResult && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30" onClick={() => setTestResult(null)}>
          <div className="bg-bg-primary rounded-xl border border-border shadow-lg p-5 max-w-sm w-full mx-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-3">
              <span className="text-title text-text-primary">{testResult.name}</span>
              <button onClick={() => setTestResult(null)} className="text-text-tertiary hover:text-text-primary">✕</button>
            </div>
            <div className="space-y-3 text-body">
              {(["video","audio"] as const).map((key) => {
                const label = key === "video" ? t("hero.table.headerVideo") : t("hero.table.headerAudio");
                const isErr = testResult[`has${key[0].toUpperCase()}${key.slice(1)}Err` as "hasVideoErr"|"hasAudioErr"];
                const txt = testResult[key];
                return (
                  <div key={key}>
                    <span className={`text-caption ${isErr ? "text-danger" : "text-text-tertiary"}`}>{label}{isErr ? ` (${t("hero.table.testFailed")})` : ""}</span>
                    <p className={`mt-1 ${isErr ? "text-danger" : !txt ? "text-text-tertiary" : "text-text-secondary"}`}>
                      {txt || (isErr ? t("hero.table.testFailed") : t("hero.table.testEmpty"))}
                    </p>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}

      {/* 开音频知情提示：opt-in。讲清可能的问题 + 适用/不适用场景。 */}
      {pendingAudioOn && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40"
          onClick={() => setPendingAudioOn(null)}>
          <div role="dialog" aria-modal="true" aria-labelledby="audio-on-title"
            className="w-[90%] max-w-sm bg-bg-secondary border border-border rounded-2xl shadow-lg p-6 anim-in"
            onClick={(e) => e.stopPropagation()}>
            <h2 id="audio-on-title" className="text-title font-semibold text-text-primary mb-2">
              {t("hero.voiceOnConfirmTitle", { name: pendingAudioOn.name })}
            </h2>
            <p className="text-body text-text-secondary mb-3">
              {t("hero.voiceOnConfirmIntro")}
            </p>
            <ul className="flex flex-col gap-2 mb-5 text-body">
              <li className="flex gap-2">
                <span className="text-warning shrink-0" aria-hidden>⚠</span>
                <span className="text-text-secondary">{t("hero.voiceOnConfirmRisk")}</span>
              </li>
              <li className="flex gap-2">
                <span className="text-success shrink-0" aria-hidden>✓</span>
                <span className="text-text-secondary">{t("hero.voiceOnConfirmRecommend")}</span>
              </li>
              <li className="flex gap-2">
                <span className="text-error shrink-0" aria-hidden>✕</span>
                <span className="text-text-secondary">{t("hero.voiceOnConfirmAvoid")}</span>
              </li>
            </ul>
            <label className="flex items-center gap-2 mb-5 text-body text-text-secondary cursor-pointer select-none">
              <input type="checkbox" checked={dontRemind}
                onChange={(e) => setDontRemind(e.target.checked)}
                className="h-4 w-4 rounded border-border accent-brand-primary cursor-pointer" />
              {t("hero.voiceOnConfirmDontRemind")}
            </label>
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => setPendingAudioOn(null)}
                className="text-body px-4 py-2 rounded-lg bg-bg-primary border border-border text-text-primary hover:border-border-strong">
                {t("hero.voiceOnConfirmCancel")}
              </button>
              <button type="button"
                onClick={() => {
                  const { run } = pendingAudioOn;
                  if (dontRemind) setAudioOnConfirmed();
                  setPendingAudioOn(null);
                  run();
                }}
                className="text-body px-4 py-2 rounded-lg font-semibold bg-brand-primary text-white hover:opacity-90">
                {t("hero.voiceOnConfirmOk")}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

/** 记住上一次的 activeCount —— useEffect 跨零检测用。 */
function usePrevious<T>(value: T): T | undefined {
  const [pair, setPair] = useState<{ prev: T | undefined; curr: T }>({ prev: undefined, curr: value });
  if (pair.curr !== value) {
    setPair({ prev: pair.curr, curr: value });
  }
  return pair.prev;
}