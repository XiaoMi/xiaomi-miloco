/**
 * Hero 摄像头实时画面组件。
 *
 * 单一 iframe(embedded mode):iframe 始终 mount 在小窗 div 内,expanded=true 时 CSS
 * 切到 fixed positioning + inline rect(来自 modal 占位 div),DOM parent 不变 →
 * WS 不重连 → 大窗瞬开。rect 没算出前不切 fixed,避免 iframe 飘到 (0,0)。
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { createPortal } from "react-dom";
import { IconCamera, IconX } from "@/lib/icons";
import { useEscClose } from "@/hooks/useEscClose";
import { resolveToken } from "@/api/client";

interface Props {
  cameraName: string;
  roomName?: string;
  cameraDid: string;
  channel: number;
  source?: "miot" | "rtsp";
  className?: string;
  disabled?: boolean;
  disabledMessage?: string;
  dimmed?: boolean;
  dimmedMessage?: string;
}

export function LivePlayerPlaceholder({
  cameraName,
  roomName,
  cameraDid,
  channel,
  source = "miot",
  className,
  disabled = false,
  disabledMessage,
  dimmed = false,
  dimmedMessage,
}: Props) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const [smallLoaded, setSmallLoaded] = useState(false);
  const modalSlotRef = useRef<HTMLDivElement>(null);
  const [rect, setRect] = useState<{
    top: number;
    left: number;
    width: number;
    height: number;
  } | null>(null);

  useEscClose(expanded, () => setExpanded(false));

  // (did, channel) 变才重算 src,平时稳定。useMemo 比 render 阶段写 ref 更标准,
  // concurrent mode 重跑 render 时也安全(useMemo 自动跟 deps 一致)。
  const refKey = `${source}|${cameraDid}|${channel}`;
  const src = useMemo(() => {
    if (source === "rtsp") {
      const token = resolveToken();
      const qs = token ? `?token=${encodeURIComponent(token)}` : "";
      return `/api/miot/rtsp_cameras/${encodeURIComponent(cameraDid)}/mjpeg${qs}`;
    }
    return `/api/miot/watch?camera_id=${encodeURIComponent(cameraDid)}&channel=${channel}&embedded=1`;
  }, [cameraDid, channel, source]);

  // refKey 变(cam 或 channel 切换)时重置 loading mask,让"拉流中…"再显一次盖
  // 旧画面到新首帧之间的空隙。避免 React 复用 LivePlayer 实例换 cam 时 mask 不出。
  useEffect(() => {
    setSmallLoaded(false);
  }, [refKey]);

  // expanded=true 时同步 modal 占位 div 的 boundingRect → iframe 用 fixed +
  // inline rect 浮上去。ResizeObserver 监听占位尺寸/位置变化(dimmedMessage chip
  // 异步出现导致 header 高度变化等),resize 兜窗口大小变化。
  useLayoutEffect(() => {
    if (!expanded) {
      setRect(null);
      return;
    }
    const update = () => {
      const el = modalSlotRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      setRect({ top: r.top, left: r.left, width: r.width, height: r.height });
    };
    update();
    window.addEventListener("resize", update);
    const ro = new ResizeObserver(update);
    if (modalSlotRef.current) ro.observe(modalSlotRef.current);
    return () => {
      window.removeEventListener("resize", update);
      ro.disconnect();
    };
  }, [expanded]);

  // expanded 期间锁 body 滚动
  useEffect(() => {
    if (!expanded) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [expanded]);

  const handleOpen = () => {
    if (disabled) return;
    setExpanded(true);
  };

  // iframe className/style: expanded=true 立刻切 fixed,rect 没算出前用临时占位
  // (跟 dialog 同款居中尺寸)铺在 modal 中心,避免 iframe 第一帧仍在小窗位置被
  // 蒙层盖住的"闪屏"。rect 算出后切到精确占位 div rect。
  const useFixed = expanded;
  // iframe expanded 时 z-[61] 让它高于 portal modal scrim z-[60],避免同 z 时
  // DOM 顺序后绘制反而盖住 iframe(modal 占位 div 是 portal 子树后绘)。
  const mediaClass = useFixed
    ? "z-[61] bg-black pointer-events-auto"
    : "absolute inset-0 w-full h-full pointer-events-none";
  const mediaStyle: React.CSSProperties = useFixed
    ? rect
      ? {
          position: "fixed",
          top: rect.top,
          left: rect.left,
          width: rect.width,
          height: rect.height,
          border: "none",
        }
      : {
          // rect 未就绪用居中临时占位(跟 dialog max-w-4xl 同款居中)
          position: "fixed",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          width: "min(90vw, 56rem)",
          aspectRatio: "16 / 9",
          border: "none",
        }
    : { border: "none" };

  return (
    <>
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-disabled={disabled}
        aria-label={t("devices.watchCamera", { name: cameraName })}
        onClick={handleOpen}
        onKeyDown={(e) => {
          if (disabled) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setExpanded(true);
          }
        }}
        className={`relative aspect-video w-full overflow-hidden rounded-xl border border-border shadow-sm focus:outline-none focus:ring-2 focus:ring-brand-primary bg-black ${
          disabled ? "cursor-default opacity-60" : "cursor-pointer"
        } ${className ?? ""}`}
      >
        {disabled ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center text-white/70 pointer-events-none">
            <IconCamera width={36} height={36} />
            <div className="mt-2 text-body opacity-90">{cameraName}</div>
            {disabledMessage && (
              <div className="text-caption opacity-60">{disabledMessage}</div>
            )}
          </div>
        ) : (
          <>
            {source === "rtsp" ? (
              <img
                src={src}
                alt={t("devices.liveView", { name: cameraName })}
                aria-hidden={useFixed ? undefined : "true"}
                className={`${mediaClass} object-cover`}
                style={mediaStyle}
                onLoad={() => setSmallLoaded(true)}
              />
            ) : (
              <iframe
                src={src}
                title={t("devices.liveView", { name: cameraName })}
                aria-hidden={useFixed ? undefined : "true"}
                className={mediaClass}
                style={mediaStyle}
                allow="autoplay"
                onLoad={() => setSmallLoaded(true)}
              />
            )}
            {!smallLoaded && !useFixed && (
              <div className="absolute inset-0 flex items-center justify-center text-text-tertiary text-caption pointer-events-none">
                {t("devices.loadingStream")}
              </div>
            )}
            {dimmed && smallLoaded && dimmedMessage && !useFixed && (
              <div className="absolute inset-0 flex items-end justify-center bg-black/30 pointer-events-none">
                <div className="mb-3 px-2 py-1 rounded-md bg-black/60 text-white/90 text-caption">
                  {dimmedMessage}
                </div>
              </div>
            )}
          </>
        )}

        {roomName && !useFixed && (
          <div className="absolute left-3 bottom-3 px-2 py-1 rounded-md bg-black/40 text-white text-caption pointer-events-none z-10">
            {roomName}
          </div>
        )}
        {!disabled && !useFixed && (
          <div className="absolute right-3 bottom-3 inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-black/40 text-white text-caption pointer-events-none z-10">
            <span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse"></span>
            LIVE
          </div>
        )}
      </div>

      {expanded &&
        createPortal(
          <div
            className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40"
            onClick={() => setExpanded(false)}
          >
            <div
              role="dialog"
              aria-modal="true"
              aria-label={t("devices.liveView", { name: cameraName })}
              className="w-full max-w-4xl max-h-[90vh] bg-bg-secondary rounded-xl border border-border shadow-sm overflow-hidden flex flex-col anim-in"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center gap-3 px-4 py-2 border-b border-border bg-bg-secondary min-w-0">
                <div className="text-title text-text-primary truncate">
                  {cameraName}
                  {roomName && (
                    <span className="text-caption text-text-secondary font-normal ml-2">
                      · {roomName}
                    </span>
                  )}
                </div>
                {dimmed && dimmedMessage && (
                  <span className="text-caption inline-flex items-center px-2 py-0.5 rounded-full bg-warning-bg text-warning shrink-0">
                    {dimmedMessage}
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => setExpanded(false)}
                  className="ml-auto rounded-full p-1.5 text-text-secondary hover:text-text-primary hover:bg-bg-primary"
                  aria-label={t("devices.close")}
                >
                  <IconX />
                </button>
              </div>
              {/* 占位 div:iframe 通过 ref 同步 rect 浮上来盖在这里。flex-1
                   min-h-0 让画面自动让位给 header,小屏不溢出。 */}
              <div ref={modalSlotRef} className="aspect-video w-full flex-1 min-h-0 bg-black" />
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}
