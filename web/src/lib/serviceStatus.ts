/**
 * 后端服务（进程）可用性探测。
 *
 * 为什么单独一个模块：slim App 的菜单栏里有「停止服务 / 启动服务」，服务停掉之后
 * 已经打开的内置网页并不会消失（WKWebView 还捧着老页面）——页面必须自己看出来
 * 「是服务停了，不是数据坏了」，服务回来后再自动刷新。全局的 `serviceDown` 标记
 * 就是给这中间那段空窗期用的：那几秒里十几个 useAsync 会集体失败，逐条弹
 * 「加载失败」toast 只会盖住真正有用的那条横幅提示，故统一静音。
 */

export type ServiceState = "checking" | "up" | "down";

let serviceDown = false;

/** 服务是否处于「已确认停止」状态（由 useServiceStatus 维护）。 */
export function isServiceDown(): boolean {
  return serviceDown;
}

export function setServiceDown(down: boolean): void {
  serviceDown = down;
}

/**
 * 单次 `/health` 探测：HTTP 2xx 即视为服务在跑。
 *
 * 不走 `apiFetch`：它要求鉴权头、按后端 `NormalResponse` 解析 body，失败还会抛
 * `ApiError` 走统一错误路径；这里只关心「进程活着没有」，也不需要 token。
 * `/health` 本身是免鉴权端点（后端 main.py 的 include_in_schema=False 健康检查）。
 */
export async function probeService(timeoutMs = 2500): Promise<boolean> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch("/health", { signal: ctl.signal, cache: "no-store" });
    return res.ok;
  } catch {
    // 连接被拒（服务已停）/ 超时 / 网络异常一律归为「不可用」——探测本身不该抛。
    return false;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * 探测结果 → 下一状态，以及是否要触发「服务恢复」回调。
 *
 * 抽成纯函数是为了可测：`down → up` 才该自动刷新页面，而 `checking → up`（正常冷启动）
 * 绝不能触发刷新 —— 否则每次开页面都会自我重载一次。测试环境没有 DOM，跑不了 hook，
 * 这条语义只有在这里才测得到。
 */
export function nextServiceState(
  prev: ServiceState,
  ok: boolean,
): { state: ServiceState; recovered: boolean } {
  return { state: ok ? "up" : "down", recovered: ok && prev === "down" };
}
