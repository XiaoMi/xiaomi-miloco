/**
 * 明细表里模型名后面那个灰字 URL 框：常显截断的 URL，点击弹出完整 URL。
 *
 * 为什么气泡用 `position: fixed` 而不是 absolute：这张表在 `overflow-x:auto` 容器里，
 * 而 CSS 规定「一轴 visible、另一轴不是 visible 时，visible 计算为 auto」——所以
 * overflow-y 实际是 auto。实测把绝对定位浮层锚在末行上：**超出容器 82px 并催出了
 * 一条竖滚动条**（scrollHeight 169 / clientHeight 86）。fixed 脱离所有裁剪上下文，
 * 代价是位置要自己算，且滚动时会失锚——故滚动即关闭。落点算法与同一行末尾操作格里的
 * 清理菜单**共用 placePopover**：先量出气泡真实尺寸，下方装不下就翻到上方，两轴夹回
 * 视口内。这个气泡与清理菜单是同一批加的，但落点最初各写了一份：这边拍一个宽度、
 * 永远贴下方、只夹右边——明细末几行贴视口底时气泡整体落在视口外，而滚动会关掉它，
 * 住户连滚下去看一眼都做不到。
 *
 * 为什么常显截断 URL 而不是只放一个彩色圆点：圆点在展开前只有颜色能区分两行，
 * 色盲 / 打印 / 强制色下就失效了；常显的字则零交互可读。
 *
 * 短形式由调用方用 shortenUrlSet 对照**全表的 URL 集合**算出，而不是本组件按固定
 * 规则自己截——差异可能落在预算之外（`…cluster-a…` 与 `…cluster-b…` 会截成同一串），
 * 只有对照实际出现的那一组才能保证可分辨。
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { placePopover } from "@/lib/popoverPlace";
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
  /** 锚点（按钮的视口坐标）：点开那一刻记下，落点由 layout effect 量完再算。 */
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);
  /** 同时包住按钮与气泡：外部点击的判据查这个节点（见 onDown 的说明）。 */
  const wrapRef = useRef<HTMLSpanElement | null>(null);
  const open = anchor !== null;

  useEffect(() => {
    if (!open) return;
    const close = () => {
      setAnchor(null);
      setPos(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close();
        btnRef.current?.focus();
      }
    };
    const onDown = (e: MouseEvent) => {
      // 判据必须覆盖**气泡本体**，不能只查按钮：气泡在 DOM 上是按钮的兄弟，
      // 而拖选文本要先 mousedown——只查按钮的话，在地址上一按就把气泡卸载了，
      // 选区无从产生、复制每次都失败。短形式是压过的，点开的下一步通常正是
      // 「把完整地址复制出去比对配置」，关掉它等于把这个气泡的用途砍掉一半。
      if (!wrapRef.current?.contains(e.target as Node)) close();
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
    if (open) {
      setAnchor(null);
      setPos(null);
      return;
    }
    const r = btnRef.current?.getBoundingClientRect();
    if (r) setAnchor(r);
  }

  // 量出气泡真实高度之后再定位：URL 用 break-all 展示，两三行就有 60-80px 高，
  // 拍一个估值必然在某些行上落到视口外。
  useLayoutEffect(() => {
    if (!anchor || !boxRef.current) return;
    const box = boxRef.current.getBoundingClientRect();
    const next = placePopover(
      anchor,
      { width: box.width, height: box.height },
      { width: window.innerWidth, height: window.innerHeight },
      { gap: 6, edge: 8, align: "left" },
    );
    setPos((p) => (p && p.left === next.left && p.top === next.top ? p : next));
  }, [anchor]);

  return (
    <span ref={wrapRef} className="inline-flex">
      <button
        ref={btnRef}
        type="button"
        onClick={toggle}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={t("usage.modelUrlAria", { url })}
        className={`num text-[11px] leading-normal px-1.5 rounded border transition-colors ${
          open
            ? "border-border-strong text-text-primary"
            : "border-border text-text-tertiary hover:text-text-secondary hover:border-border-strong"
        }`}
      >
        {label}
      </button>

      {open && (
        <div
          ref={boxRef}
          role="dialog"
          aria-label={t("usage.modelUrlAria", { url })}
          style={{
            width: 320,
            // 落点未算出前先摆到锚点下方并隐形：要先渲染才量得到高度
            left: pos ? pos.left : (anchor?.left ?? 0),
            top: pos ? pos.top : (anchor?.bottom ?? 0) + 6,
            visibility: pos ? undefined : "hidden",
          }}
          className="fixed z-[80] rounded-xl border border-border bg-bg-secondary
                     shadow-lg p-3 text-caption"
        >
          {/* 完整原文，不截断 */}
          <p className="num text-text-secondary break-all">{url}</p>
        </div>
      )}
    </span>
  );
}
