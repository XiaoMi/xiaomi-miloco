/**
 * 明细表里模型名后面那个灰字 URL 框：常显截断的 URL，点击弹出完整 URL。
 *
 * 为什么气泡用 `position: fixed` 而不是 absolute：这张表在 `overflow-x:auto` 容器里，
 * 而 CSS 规定「一轴 visible、另一轴不是 visible 时，visible 计算为 auto」——所以
 * overflow-y 实际是 auto。实测把绝对定位浮层锚在末行上：**超出容器 82px 并催出了
 * 一条竖滚动条**（scrollHeight 169 / clientHeight 86）。fixed 脱离所有裁剪上下文，
 * 代价是位置要自己算，且滚动时会失锚——故滚动即关闭。
 *
 * 为什么常显截断 URL 而不是只放一个彩色圆点：圆点在展开前只有颜色能区分两行，
 * 色盲 / 打印 / 强制色下就失效了；常显的字则零交互可读。
 *
 * 短形式由调用方用 shortenUrlSet 对照**全表的 URL 集合**算出，而不是本组件按固定
 * 规则自己截——差异可能落在预算之外（`…cluster-a…` 与 `…cluster-b…` 会截成同一串），
 * 只有对照实际出现的那一组才能保证可分辨。
 */

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
export function UsageUrlChip({
  url,
  label,
}: {
  /** 本行记录的 Base URL 原文。空串走「旧版本数据未记录」分支，由调用方判断后不传本组件。 */
  url: string;
  /** 灰字框里显示的短形式，由 shortenUrlSet 对照全表 URL 集合算出（保证互不相同）。 */
  label: string;
}) {
  const { t } = useTranslation();
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const open = pos !== null;

  useEffect(() => {
    if (!open) return;
    const close = () => setPos(null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close();
        btnRef.current?.focus();
      }
    };
    const onDown = (e: MouseEvent) => {
      if (!btnRef.current?.contains(e.target as Node)) close();
    };
    // fixed 定位在滚动后会失锚（页面滚了，气泡不动）→ 直接关掉，不做跟随
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function toggle() {
    if (open) return setPos(null);
    const r = btnRef.current?.getBoundingClientRect();
    if (!r) return;
    const W = 320;
    setPos({
      // 右边贴视口时往左收，避免气泡跑到屏幕外
      left: Math.min(r.left, window.innerWidth - W - 12),
      top: r.bottom + 6,
    });
  }

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        onClick={toggle}
        aria-expanded={open}
        aria-label={t("usage.modelUrlAria")}
        className={`num text-[11px] leading-normal px-1.5 rounded border transition-colors ${
          open
            ? "border-border-strong text-text-primary"
            : "border-border text-text-tertiary hover:text-text-secondary hover:border-border-strong"
        }`}
      >
        {label}
      </button>

      {pos && (
        <div
          role="dialog"
          aria-label={t("usage.modelUrlAria")}
          style={{ left: pos.left, top: pos.top, width: 320 }}
          className="fixed z-[80] rounded-xl border border-border bg-bg-secondary
                     shadow-lg p-3 text-caption"
        >
          {/* 完整原文，不截断 */}
          <p className="num text-text-secondary break-all">{url}</p>
        </div>
      )}
    </>
  );
}
