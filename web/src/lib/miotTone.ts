/**
 * 米家连接项的三态判据。
 *
 * 抽成纯函数是为了能被单测钉住：状态条与账号按钮两处各自实现过一遍，其中一处
 * 曾把失效档写成 `bound && authDegraded`，于是 access_token 也过期后 bound 翻
 * false，问题变得更严重的那一刻反而从红退回黄「未连」——而同一刻命令行体检正确
 * 报不通过，两个面的口径劈叉。判据只此一份，两处都从这里取。
 */

/** 授权失效优先级最高，与命令行体检的分支顺序一致。 */
export type MiotTone = "degraded" | "connected" | "disconnected";

export function miotTone(input: {
  bound: boolean;
  authDegraded?: boolean;
}): MiotTone {
  // 失效**独立于 bound**：续期被云端拒绝、而 access_token 还没到期时 bound 仍是
  // true；等它也过期 bound 翻 false，但授权依然是失效的——两种情况都该显示失效。
  if (input.authDegraded) return "degraded";
  return input.bound ? "connected" : "disconnected";
}
