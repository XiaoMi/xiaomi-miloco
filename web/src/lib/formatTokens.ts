// 用量数字的住户友好格式化。所有显示 token 数的地方共用一份——用量页的总览、明细、
// 时间分布、收起简报，以及首页那处——否则同一笔 tokens 在不同卡片显示精度不一，
// 会触发"是不是数字跳了"的视觉抖动。
//
// 阈值与保留位数：
//   < 1k       → 整数（"850"）
//   1k - 1M    → "X.Xk"（保留 1 位小数）
//   ≥ 1M       → "X.XXM"（保留 2 位小数，避免精度损失）

export function humanTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return `${Math.round(n)}`;
}

// 坐标轴刻度专用：整数不带小数点，非整数保留一位。
// 纵轴上限取 1/2/5×10ⁿ 的漂亮数，中线因此可能是 2.5×10ⁿ 这种半数，
// 必须如实标成 "2.5k"，否则标注与刻度线位置对不上。
export function axisTokens(n: number): string {
  if (n >= 1_000_000) {
    const v = n / 1_000_000;
    return `${Number.isInteger(v) ? v : v.toFixed(1)}M`;
  }
  if (n >= 1_000) {
    const v = n / 1_000;
    return `${Number.isInteger(v) ? v : v.toFixed(1)}k`;
  }
  // 与千以上两支同口径：非整数如实保留一位。纵轴上限取 1/2/5×10ⁿ，小数值下中线会
  // 落在半数（如上限 5 时中线 2.5），四舍五入会让标注与它标的那条网格线错位。
  return `${Number.isInteger(n) ? n : n.toFixed(1)}`;
}
