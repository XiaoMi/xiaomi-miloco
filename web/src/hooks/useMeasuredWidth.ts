import { useLayoutEffect, useRef, useState } from "react";

/**
 * 量出容器的 CSS 像素宽，给 SVG 图表当 viewBox 宽度用。
 *
 * 为什么图表需要这个：viewBox 取归一化常量（如 1000）配 `preserveAspectRatio="none"`
 * 时，水平方向按 `容器宽 / 常量` 缩放，于是 SVG 里的左边距（PAD_L 个单位）在不同容器宽
 * 下落在不同的像素位置；而 y 轴刻度标签是 HTML 浮层、**按像素**定宽。两套单位在容器变窄
 * 时脱钩——标签越过绘图区左沿，被网格线压住。临界宽度约为 `(PAD_L - 6) / PAD_L × 常量`，
 * 各图落在 864~893px 之间（PAD_L 44~56）：并排显示时每张只有半卡宽，窄视口下整宽单列
 * 也照样低于它。
 *
 * 让 viewBox 宽度等于实测像素宽，SVG 单位就等于 CSS 像素，这类脱钩在结构上不再可能发生，
 * 而各图的坐标公式一行都不用改。
 *
 * 返回 `[ref, width]`：ref 挂到那个宽度要被测量的容器上。测量走 useLayoutEffect，在首次
 * 绘制前完成——用 useEffect 的话首帧会拿兜底常量画一次：曲线形状不受影响（等比缩放），
 * 但网格线落在 `PAD_L × 容器宽 / 1000`，窄容器下会短暂闪出正是本改造要修的那种压字。
 * 兜底常量因此只在拿不到元素或量出 0 时才用得上，取各图原先的归一化值。
 */
export function useMeasuredWidth(fallback = 1000) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [measured, setMeasured] = useState(0);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    // 先量一次再看有没有 ResizeObserver，顺序与时间分布图那份一致：缺了就只是不再跟随
    // 容器变化，那一次的真实宽度仍然拿到手——而不是一路吃兜底常量（吃兜底就等于回到
    // 纵轴标签被网格线压住的老样子），也不会在副作用里抛异常。
    //
    // 两处都读 clientWidth、观察者只当「变了」的触发器：这样立即测量与后续测量取的是
    // 同一个量。前提是被测元素没有横向内边距（clientWidth 含 padding，contentRect 不含）
    // ——七个调用点都是 `relative w-full` 的裸容器。将来给它加内边距的话，这里要改成
    // 减去 padding，否则 viewBox 会比绘图区宽。
    const measure = () => {
      const next = el.clientWidth;
      if (next > 0) setMeasured(Math.round(next));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return [ref, measured || fallback] as const;
}
