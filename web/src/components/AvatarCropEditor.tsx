/**
 * 头像裁剪编辑器（手写，纯 React + canvas + pointer，不引库）。
 * 圆形取景框内定位一张源图：拖动平移、滚轮 / 滑杆缩放，图像始终铺满取景框。
 * 有 initialBox（如 grounding 头部归一化 [x,y,w,h]）时，初始把该框贴合取景框；
 * 否则居中 cover。确认时把可见区域绘到 OUT×OUT 离屏 canvas → JPEG blob。
 *
 * 上传头像与「自动生成外观描述」两条路线都经本组件确认，产物统一是裁好的方图 blob。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useEscClose } from "@/hooks/useEscClose";
import { IconX } from "@/lib/icons";

const FRAME = 240; // 取景框边长（px）
const OUT = 256; // 输出方图边长（px）
const MAX_ZOOM = 4;

interface Props {
  /** 源图：上传的文件 或 base64（自动生成的 crop）。 */
  source: { file: File } | { b64: string };
  /** 头部等归一化初始框 [x,y,w,h]（相对源图），用作默认裁剪范围。 */
  initialBox?: number[] | null;
  onCancel: () => void;
  onConfirm: (blob: Blob) => void;
}

export function AvatarCropEditor({
  source,
  initialBox,
  onCancel,
  onConfirm,
}: Props) {
  const { t } = useTranslation();
  const src = useMemo(() => {
    if ("b64" in source) {
      // 仅接受 base64 字符再拼 data URL，挡住畸形/注入输入（避免 DOM 文本被当 HTML）
      const b64 = /^[A-Za-z0-9+/=]*$/.test(source.b64) ? source.b64 : "";
      return `data:image/jpeg;base64,${b64}`;
    }
    return URL.createObjectURL(source.file);
  }, [source]);
  useEffect(() => {
    // 仅 file 分支创建了 objectURL，需回收
    return () => {
      if ("file" in source) URL.revokeObjectURL(src);
    };
  }, [src, source]);

  const imgRef = useRef<HTMLImageElement>(null);
  const frameRef = useRef<HTMLDivElement>(null);
  const [dims, setDims] = useState<{ w: number; h: number } | null>(null);
  const [zoom, setZoom] = useState(1); // ≥1 的缩放倍数（相对 coverScale）
  const [pos, setPos] = useState({ x: 0, y: 0 }); // 图像左上相对取景框左上的位移（px）
  const drag = useRef<{ px: number; py: number; x: number; y: number } | null>(
    null,
  );

  useEscClose(true, onCancel);

  // coverScale：让源图恰好铺满取景框的最小缩放（frame px / natural px）
  const coverScale = dims ? Math.max(FRAME / dims.w, FRAME / dims.h) : 1;
  const scale = coverScale * zoom;

  // 钳制位移使图像始终铺满取景框；可显式传 d（首帧 dims 尚未入 state 时用局部值）。
  const clampWith = (
    p: { x: number; y: number },
    s: number,
    d: { w: number; h: number } | null,
  ) => {
    if (!d) return p;
    return {
      x: Math.min(0, Math.max(FRAME - d.w * s, p.x)),
      y: Math.min(0, Math.max(FRAME - d.h * s, p.y)),
    };
  };
  const clampPos = (p: { x: number; y: number }, s: number) =>
    clampWith(p, s, dims);

  // 图片加载完成：读自然尺寸，按 initialBox / 居中算初始缩放与位移
  const onImgLoad = () => {
    const el = imgRef.current;
    if (!el) return;
    const w = el.naturalWidth;
    const h = el.naturalHeight;
    setDims({ w, h });
    const cover = Math.max(FRAME / w, FRAME / h);
    let s = cover;
    let cx = w / 2;
    let cy = h / 2;
    if (initialBox && initialBox.length === 4) {
      const [bx, by, bw, bh] = initialBox;
      const boxW = Math.max(1, bw * w);
      const boxH = Math.max(1, bh * h);
      cx = (bx + bw / 2) * w;
      cy = (by + bh / 2) * h;
      // 让头部框以 ~85% 占满取景框，但不低于 cover（保证铺满）
      s = Math.min(
        MAX_ZOOM * cover,
        Math.max(cover, (FRAME * 0.85) / Math.max(boxW, boxH)),
      );
    }
    setZoom(s / cover);
    setPos(clampWith({ x: FRAME / 2 - cx * s, y: FRAME / 2 - cy * s }, s, { w, h }));
  };

  // 缩放时保持取景框中心对应的图像点不动
  const applyZoom = (nextZoom: number) => {
    if (!dims) return;
    const z = Math.min(MAX_ZOOM, Math.max(1, nextZoom));
    const sOld = coverScale * zoom;
    const sNew = coverScale * z;
    const centerImgX = (FRAME / 2 - pos.x) / sOld;
    const centerImgY = (FRAME / 2 - pos.y) / sOld;
    setZoom(z);
    setPos(
      clampPos(
        { x: FRAME / 2 - centerImgX * sNew, y: FRAME / 2 - centerImgY * sNew },
        sNew,
      ),
    );
  };

  // 滚轮缩放（非 passive，阻止页面滚动）
  useEffect(() => {
    const el = frameRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      applyZoom(zoom * (1 - e.deltaY * 0.0015));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  });

  const onPointerDown = (e: React.PointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { px: e.clientX, py: e.clientY, x: pos.x, y: pos.y };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    setPos(
      clampPos(
        { x: d.x + (e.clientX - d.px), y: d.y + (e.clientY - d.py) },
        scale,
      ),
    );
  };
  const onPointerUp = (e: React.PointerEvent) => {
    drag.current = null;
    e.currentTarget.releasePointerCapture(e.pointerId);
  };

  const confirm = () => {
    const el = imgRef.current;
    if (!el || !dims) return;
    // 取景框左上/尺寸映射回源图自然像素
    const sx = -pos.x / scale;
    const sy = -pos.y / scale;
    const sSize = FRAME / scale;
    const canvas = document.createElement("canvas");
    canvas.width = OUT;
    canvas.height = OUT;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.drawImage(el, sx, sy, sSize, sSize, 0, 0, OUT, OUT);
    canvas.toBlob(
      (blob) => {
        if (blob) onConfirm(blob);
      },
      "image/jpeg",
      0.9,
    );
  };

  return (
    <div
      className="fixed inset-0 z-[80] flex items-end md:items-center justify-center bg-black/50"
      onClick={onCancel}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("avatar.cropTitle")}
        className="w-full max-w-sm bg-bg-secondary border border-border rounded-t-2xl md:rounded-xl shadow-sm p-6 anim-in"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-title text-text-primary">{t("avatar.cropTitle")}</h3>
          <button
            type="button"
            onClick={onCancel}
            className="rounded-full p-1 text-text-secondary hover:text-text-primary"
            aria-label={t("avatar.cancel")}
          >
            <IconX />
          </button>
        </div>

        <div className="flex flex-col items-center gap-3">
          <div
            ref={frameRef}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            style={{ width: FRAME, height: FRAME }}
            className="relative overflow-hidden rounded-full border border-border bg-bg-tertiary touch-none cursor-grab active:cursor-grabbing select-none"
          >
            {/* eslint-disable-next-line jsx-a11y/alt-text */}
            <img
              ref={imgRef}
              src={src}
              onLoad={onImgLoad}
              draggable={false}
              alt=""
              style={{
                position: "absolute",
                left: pos.x,
                top: pos.y,
                width: dims ? dims.w * scale : undefined,
                height: dims ? dims.h * scale : undefined,
                maxWidth: "none",
              }}
            />
          </div>
          <p className="text-caption text-text-tertiary">{t("avatar.cropHint")}</p>

          <div className="w-full flex items-center gap-3">
            <span className="text-caption text-text-secondary shrink-0">
              {t("avatar.cropZoom")}
            </span>
            <input
              type="range"
              min={1}
              max={MAX_ZOOM}
              step={0.01}
              value={zoom}
              onChange={(e) => applyZoom(Number(e.target.value))}
              className="flex-1 accent-brand-primary"
            />
          </div>

          <button
            type="button"
            onClick={confirm}
            disabled={!dims}
            className="w-full py-2 rounded-lg bg-brand-primary text-white hover:bg-brand-accent disabled:opacity-60"
          >
            {t("avatar.cropConfirm")}
          </button>
        </div>
      </div>
    </div>
  );
}
