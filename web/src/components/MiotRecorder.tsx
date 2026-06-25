/**
 * 用家里 miloco 摄像头录一段 → 上传 /extract。
 *
 * 实现:
 *   1. iframe 嵌 backend 的 /api/miot/watch 仅做"取景器"。用户看到画面 →
 *      知道镜头框住了什么 → 点录制。iframe 不再参与录像本身。
 *   2. 点录制 → MiOT 摄像头走 /api/miot/record_clip,RTSP 摄像头走
 *      /api/miot/rtsp_cameras/{did}/record_clip。两条后端路径都返回 mp4 blob。
 *   3. blob 直接喂回 onDone() → EnrollFlow 走 /extract 拿候选帧。
 *
 * 相比"在浏览器侧抓 canvas + MediaRecorder"的老方案:
 *   - 不再依赖 watch.html 的 canvas 解码(老路径在不同浏览器表现不稳定,
 *     Bug 3 就是这一段卡死)。
 *   - 后端录制器作为第二种 subscriber 注册,SDK 仍只 reg 一次,不会触发
 *     "同 camera 双订阅"。
 *
 * 注:后端录制走的是 SDK 已解码 BGR → libx264 ultrafast/zerolatency 二次
 * 编码,**不是** NAL 原样 remux —— 原方案在 PyAV 17 的 raw h264 demuxer
 * 上拿不出包,详见 NalClipRecorder docstring。
 */

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { PerceptionCamera } from "@/lib/types";
import { authHeaders } from "@/api/register";
import { resolveToken } from "@/api/client";

interface Props {
  cameras: PerceptionCamera[];
  onDone: (blob: Blob) => void;
  onCancel: () => void;
}

const RECORD_SECONDS = 15;
// 后端总耗时 ≈ 首帧 BGR(SDK 已解码就开录,不等 IDR) + 录制时长 + libx264
// flush + mp4 mux(<1s),内部超时 = 录制时长 + 8s。前端再加 5s 余量,留出
// 响应序列化 + 网络回程的时间——否则前后端在同一 t 触发超时,后端 504 文案
// 会被前端的 AbortError 静默分支吞掉,用户只看到 preview 又冒出来。
const FETCH_TIMEOUT_MS = (RECORD_SECONDS + 8 + 5) * 1000;

export function resolveRecorderEndpoints(
  did: string,
  channel: number,
  durationMs: number,
): { previewUrl: string; recordUrl: string; previewKind: "iframe" | "mjpeg" } {
  if (did.startsWith("rtsp:")) {
    const base = `/api/miot/rtsp_cameras/${encodeURIComponent(did)}`;
    return {
      previewUrl: `${base}/mjpeg`,
      recordUrl: `${base}/record_clip?duration_ms=${durationMs}`,
      previewKind: "mjpeg",
    };
  }
  return {
    previewUrl:
      `/api/miot/watch?camera_id=${encodeURIComponent(did)}` +
      `&channel=${channel}&embedded=1`,
    recordUrl:
      `/api/miot/record_clip?camera_id=${encodeURIComponent(did)}` +
      `&channel=${channel}&duration_ms=${durationMs}`,
    previewKind: "iframe",
  };
}

export function MiotRecorder({ cameras, onDone, onCancel }: Props) {
  const { t } = useTranslation();
  const [selectedDid, setSelectedDid] = useState<string>(cameras[0]?.did ?? "");
  // preview:可以点录制
  // recording:fetch 在跑,显示假进度条
  // processing:fetch 已回,onDone 已交出 blob
  const [stage, setStage] = useState<"preview" | "recording" | "processing">(
    "preview",
  );
  // recording 阶段的进度秒数。后端先等 IDR 再开录,所以前 ~1s 可能是占位时间,
  // 用户感觉录制条件不上,但总时长基本固定 ≈ RECORD_SECONDS。
  const [recElapsed, setRecElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [iframeReady, setIframeReady] = useState(false);

  const channel = 0; // 第一版只用通道 0
  const endpoints = selectedDid
    ? resolveRecorderEndpoints(selectedDid, channel, RECORD_SECONDS * 1000)
    : null;
  const previewUrl = endpoints
    ? endpoints.previewKind === "mjpeg"
      ? (() => {
          const token = resolveToken();
          const qs = token ? `?token=${encodeURIComponent(token)}` : "";
          return `${endpoints.previewUrl}${qs}`;
        })()
      : endpoints.previewUrl
    : "";

  // 录制中允许中止:abort 立即掐断前端 fetch、回到 preview。注意后端 record_clip
  // 不检测客户端断开(无 is_disconnected 轮询),录制器会继续跑到收满 duration
  // 或超时,才在 finally 里 unregister——中途的 libx264 编码白做、响应被丢弃。
  const abortRef = useRef<AbortController | null>(null);
  // 区分"超时 abort"与"用户主动取消":两者都触发 AbortError,但只有超时该报错。
  const timedOutRef = useRef(false);
  const unmountedRef = useRef(false);

  useEffect(() => {
    setIframeReady(false);
    setError(null);
  }, [selectedDid]);

  // 录制中每秒计数(纯前端 UI 进度;后端真正的录制时长由 duration_ms 决定)
  useEffect(() => {
    if (stage !== "recording") {
      setRecElapsed(0);
      return;
    }
    setRecElapsed(0);
    const id = setInterval(() => setRecElapsed((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, [stage]);

  useEffect(() => {
    // StrictMode 在 dev 下会 mount → unmount → mount 一次。ref 不像 state
    // 不会被 React 重置,所以必须在 mount 时显式回 false,否则第一次 strict-mode
    // 模拟 unmount 会把 unmountedRef 翻到 true,后面 fetch 真的拿到 mp4 之后
    // ``if (unmountedRef.current) return;`` 直接 bail,onDone 永不触发 → 上层
    // 看不到 blob,/extract 不发,流程卡死。
    unmountedRef.current = false;
    return () => {
      unmountedRef.current = true;
      abortRef.current?.abort();
    };
  }, []);

  const start = async () => {
    if (!selectedDid) return;
    setError(null);
    setStage("recording");
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    timedOutRef.current = false;
    const timeoutId = setTimeout(() => {
      timedOutRef.current = true;
      ctrl.abort();
    }, FETCH_TIMEOUT_MS);
    try {
      const r = await fetch(endpoints?.recordUrl ?? "", {
        method: "POST",
        headers: authHeaders(),
        signal: ctrl.signal,
      });
      if (!r.ok) {
        let detail = t("account.recordFailHttp", { status: r.status });
        try {
          const j = await r.json();
          detail = j.detail ?? j.message ?? detail;
        } catch {
          /* 非 JSON 响应,沿用默认文案 */
        }
        throw new Error(detail);
      }
      const blob = await r.blob();
      if (unmountedRef.current) return;
      if (blob.size === 0) {
        throw new Error(t("account.emptyMp4"));
      }
      setStage("processing");
      onDone(blob);
    } catch (e) {
      if (unmountedRef.current) return;
      if ((e as Error).name === "AbortError") {
        // 用户取消会先 unmount(上面 unmountedRef 处已 bail),所以走到这里的 abort
        // 基本只剩超时:给一句文案,否则进度条走满后无声退回 preview,用户不知所措。
        if (timedOutRef.current) {
          setError(t("account.recordTimeout"));
        }
        setStage("preview");
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
      setStage("preview");
    } finally {
      clearTimeout(timeoutId);
      abortRef.current = null;
    }
  };

  const handleCancel = () => {
    abortRef.current?.abort();
    onCancel();
  };

  return (
    <div>
      {cameras.length === 0 ? (
        <div className="rounded-xl bg-bg-primary text-text-secondary py-6 px-4 text-center">
          {t("account.noCamera")}
        </div>
      ) : (
        <>
          {cameras.length > 1 && (
            <div className="mb-3">
              <label className="text-caption text-text-secondary mr-2">
                {t("account.whichCamera")}
              </label>
              <select
                value={selectedDid}
                onChange={(e) => setSelectedDid(e.target.value)}
                disabled={stage !== "preview"}
                className="text-caption px-2 py-1 rounded-lg bg-bg-primary border border-border text-text-primary"
              >
                {cameras.map((c) => (
                  <option key={c.did} value={c.did}>
                    {c.name}
                    {c.roomName ? ` · ${c.roomName}` : ""}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div className="rounded-xl overflow-hidden bg-black mb-3 aspect-video relative">
            {previewUrl && endpoints?.previewKind === "mjpeg" ? (
              <img
                src={previewUrl}
                alt={t("account.watchTitle")}
                className="w-full h-full object-contain"
                onLoad={() => setIframeReady(true)}
              />
            ) : previewUrl ? (
              <iframe
                src={previewUrl}
                title={t("account.watchTitle")}
                className="w-full h-full"
                style={{ border: "none" }}
                onLoad={() => setIframeReady(true)}
              />
            ) : null}
            {stage === "recording" && (
              <>
                <div className="absolute bottom-5 left-3 px-3 py-1.5 rounded-lg bg-error text-white inline-flex items-center gap-2 shadow-lg">
                  <span className="w-3 h-3 rounded-full bg-white animate-pulse" />
                  <span className="text-sm font-semibold tracking-wide">
                    REC · {Math.min(recElapsed, RECORD_SECONDS)}/{RECORD_SECONDS}s
                  </span>
                </div>
                <div className="absolute inset-0 pointer-events-none border-4 border-error rounded-xl animate-pulse" />
                <div className="absolute bottom-0 left-0 right-0 h-1.5 bg-black/40">
                  <div
                    className="h-full bg-error transition-[width] duration-1000 ease-linear"
                    style={{
                      width: `${Math.min(100, (recElapsed / RECORD_SECONDS) * 100)}%`,
                    }}
                  />
                </div>
              </>
            )}
            {stage === "processing" && (
              <div
                className="absolute bottom-3 left-3 px-3 py-1.5 rounded-lg text-white inline-flex items-center gap-2 shadow-lg"
                style={{ background: "rgba(0,0,0,0.65)" }}
              >
                <span className="w-3 h-3 rounded-full bg-white animate-pulse" />
                <span className="text-sm font-semibold tracking-wide">
                  {t("account.analyzing")}
                </span>
              </div>
            )}
          </div>

          {error && (
            <div
              className="text-caption rounded-lg bg-warning-bg border border-warning text-warning p-3 mb-3"
            >
              {error}
            </div>
          )}

          <div className="flex justify-between items-center">
            <div className="text-caption text-text-secondary">
              {stage === "preview"
                ? iframeReady
                  ? t("account.previewReady")
                  : t("account.previewLoading")
                : stage === "recording"
                  ? t("account.recordingHint")
                  : t("account.processing")}
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={handleCancel}
                disabled={stage === "processing"}
                className="px-4 py-2 rounded-lg bg-bg-primary border border-border text-text-secondary disabled:opacity-60"
              >
                {t("account.recorderCancel")}
              </button>
              {stage === "preview" && (
                <button
                  type="button"
                  onClick={start}
                  disabled={!selectedDid}
                  className="px-4 py-2 rounded-lg bg-brand-primary text-white hover:bg-brand-accent disabled:opacity-60"
                >
                  {t("account.startRecord", { seconds: RECORD_SECONDS })}
                </button>
              )}
              {stage === "recording" && (
                <button
                  type="button"
                  onClick={() => abortRef.current?.abort()}
                  disabled={recElapsed < 3}
                  className="px-4 py-2 rounded-lg bg-error text-white hover:opacity-90 disabled:opacity-60"
                  title={t("account.abortRecordTitle")}
                >
                  {t("account.abortRecord")}
                </button>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
