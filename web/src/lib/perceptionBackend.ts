/**
 * 感知后端卡片的判定逻辑。
 *
 * 抽出来是为了能测:仓库的前端测试跑在 node 环境(没有 jsdom),渲染断言做不了。
 * 这两处判定错了都有实际后果 —— 一个会悄悄清掉用户已存的凭证,另一个会在边车
 * 拒绝凭证时仍然显示绿灯。
 */

import type { PerceptionBackendState } from "./types";

export type BackendKind = "cloud" | "local";

export interface SwitchPayload {
  backend: BackendKind;
  base_url?: string;
  token?: string;
}

/**
 * 构造切换请求体。
 *
 * token 只在用户**确实填了**的时候才带上:后端把空字符串当作"清空凭证",
 * 每次切换都无条件提交 token 字段的话,一个只想换后端的用户会连带把已保存的
 * 凭证抹掉,而界面上没有任何提示。
 */
export function buildSwitchPayload(
  backend: BackendKind,
  baseUrl: string,
  token: string,
): SwitchPayload {
  // 切回云端时**刻意**不带地址与凭证:写 base_url 会触发后端的强制探活,而边车
  // 此刻很可能正是坏的 —— 那样切回云端就会被 400 挡住,把用户关在一个不工作的
  // 后端里,而这个端点是唯一的出口。地址仍留在输入框里,下次切本地时再提交。
  if (backend !== "local") return { backend };
  const tok = token.trim();
  return { backend, base_url: baseUrl.trim(), ...(tok ? { token: tok } : {}) };
}

export type HealthLine =
  | { kind: "unreachable" }
  | { kind: "auth-rejected" }
  | { kind: "loading" }
  | { kind: "load-failed"; detail: string }
  | { kind: "ok"; device: string; backend: string; gateOff: boolean }
  | { kind: "none" };

/**
 * 本地 GPU 按钮上那个「可达」角标。
 *
 * 必须与 {@link healthLine} 同源,否则同一屏上会出现按钮显示绿色「可达」、
 * 紧邻的一行显示「✗ 边车拒绝当前凭证」这种自相矛盾。
 */
export function isReachable(state: PerceptionBackendState): boolean {
  return healthLine(state).kind === "ok";
}

/**
 * 探活那一行显示什么。
 *
 * 「凭证被拒」必须是独立的一档:边车在凭证不对时仍然回 200 + model_loaded,
 * 只看 health 是否存在就会一路绿灯,而每一窗推理都在 401 —— 感知静默停摆,
 * 界面上却什么都看不出来。
 */
export function healthLine(state: PerceptionBackendState): HealthLine {
  if (state.error) return { kind: "unreachable" };
  const h = state.health;
  if (!h) return { kind: "none" };
  if (h.auth_required && !h.auth_ok) return { kind: "auth-rejected" };
  // 加载**失败**与"还在加载"必须分开:前者永远不会好(多半是权重路径写错),
  // 把它显示成"稍后再试"等于劝用户一直等下去。边车用 load_error 区分二者。
  if (!h.model_loaded && h.load_error) {
    return { kind: "load-failed", detail: h.load_error };
  }
  if (!h.model_loaded) return { kind: "loading" };
  return {
    kind: "ok",
    device: h.device ?? "",
    backend: h.backend ?? "",
    gateOff: !h.gate_available,
  };
}
