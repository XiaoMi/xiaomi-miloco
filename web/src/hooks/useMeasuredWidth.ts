import { useEffect, useRef, useState } from "react";

/**
 * 量出容器的 CSS 像素宽，给 SVG 图表当 viewBox 宽度用。
 *
 * 为什么图表需要这个：viewBox 取归一化常量（如 1000）配 `preserveAspectRatio="none"`
 * 时，水平方向按 `容器宽 / 常量` 缩放，于是 SVG 里的左边距（PAD_L 个单位）在不同容器宽
 * 下落在不同的像素位置；而 y 轴刻度标签是 HTML 浮层、**按像素**定宽。两套单位在容器变窄
 * 时脱钩——标签越过绘图区左沿，被网格线压住。临界宽度约为 `(PAD_L - 6) / PAD_L × 常量`，
 * 各图落在 860~890px 之间：并排显示时每张只有半卡宽，窄视口下整宽单列也照样低于它。
 *
 * 让 viewBox 宽度等于实测像素宽，SVG 单位就等于 CSS 像素，这类脱钩在结构上不再可能发生，
 * 而各图的坐标公式一行都不用改。
 *
 * 返回 `[ref, width]`：ref 挂到那个宽度要被测量的容器上。首帧还没量到时返回 fallback，
 * 取各图原先的归一化常量，最坏情况不比改造前差。
 */
export function useMeasuredWidth(fallback = 1000) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [measured, setMeasured] = useState(0);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect.width ?? 0;
      if (next > 0) setMeasured(Math.round(next));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return [ref, measured || fallback] as const;
}
