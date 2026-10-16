import { logger } from "../utils/logger.js";
import { runShell } from "../utils/shell.js";
import { serviceHealth } from "./health.js";
import type { ServiceBuilder } from "./index.js";

/**
 * 一次后台 restart 的在飞状态。
 *
 * 为什么 start() 不等 CLI 跑完：宿主在「安装 / reload」路径只给 service.start()
 * 5 秒（openclaw ≥2026.9.1 的 PLUGIN_SERVICE_REPLACEMENT_STOP_TIMEOUT_MS），而
 * `miloco-cli service restart` 实测 13~17s（Linux 等 supervisord startsecs + /health，
 * macOS 上 15s 全在等 /health）。超时会连插件代一起回滚，回滚后的恢复路径还会再撞一次。
 * 所以这里触发即返回，成败改走 serviceHealth 上报。
 *
 * AbortController 必须每次 start() 新建：替换失败时宿主会对同一个 service 再调一次
 * start()（恢复路径），复用已 abort 的 controller 会让 spawn 直接失败。
 */
interface Inflight {
  ac: AbortController;
  state: { aborted: boolean };
  done: Promise<void>;
}

export const createBackendService: ServiceBuilder = () => {
  let inflight: Inflight | null = null;

  /** 掐掉在飞的 restart，返回被掐掉那条的 promise（对已结束的是 no-op）。 */
  const abortInflight = (): Promise<void> | null => {
    const entry = inflight;
    if (!entry || entry.state.aborted) return null;
    entry.state.aborted = true;
    entry.ac.abort();
    return entry.done;
  };

  return {
    id: "miloco-backend",
    start: (ctx) => {
      void abortInflight(); // 重入保护：上一次还在飞就先作废

      const ac = new AbortController();
      const state = { aborted: false };
      const done = runShell("miloco-cli", ["service", "restart"], {
        signal: ac.signal,
        // abort 一律 SIGKILL：CLI 没有清理义务（拆解是 service stop 的活），
        // 让下面 stop() 的 await 有界 —— stop 超时对宿主是**硬失败**上报。
        killSignal: "SIGKILL",
      }).then((result) => {
        if (state.aborted) {
          // 我们自己掐的，不算成败：不报失败（宿主此刻已 revoke 这个 reporter，
          // 报了也会被丢），也别打 ❌ 误导读日志的人。
          logger.warn("⚠️ start miloco-backend 被 stop 掐断，未跑完");
          return;
        }
        if (result.error || result.status !== 0) {
          logger.error(
            `❌ start miloco-backend, details:${JSON.stringify(result)}`,
          );
          serviceHealth(ctx)?.reportFailure(
            new Error(
              `miloco-cli service restart failed: ${JSON.stringify(result)}`,
            ),
          );
        } else {
          logger.info(
            `✅ start miloco-backend, details:${JSON.stringify(result)}`,
          );
          serviceHealth(ctx)?.clearFailure();
        }
      }).catch((err) => {
        // 上报口/logger 自身抛错不能让 rejection 逃逸：start 早就返回了，这条 promise
        // 可能永远没人 await（只有 stop() 会），逃出去就是一个 unhandled rejection。
        // runShell 本身不会 reject，能走到这里只可能是上面回调里抛的。
        logger.error(`❌ miloco-backend 后台收尾异常: ${String(err)}`);
      });
      inflight = { ac, state, done };

      // 不 return done —— 宿主只给 5s，返回即视为启动完成。
      logger.info("miloco-backend start 已触发（后台等待 miloco-cli restart）");
    },
    stop: async () => {
      // 先掐再停，顺序不能反：宿主在 reload 失败时会紧接着调 stop()，不掐的话那条
      // 在飞的 restart 可能在 service stop 拆完之后才把 supervisord/后端重新拉起来，
      // 而 stop 每次启动尝试最多只调一次、失败宿主也不重试 —— 会留下没人认领的后端。
      const aborted = abortInflight();
      if (aborted) await aborted;

      const result = await runShell("miloco-cli", ["service", "stop"]);
      if (result.error || result.status !== 0) {
        logger.error(
          `❌ stop miloco-backend, details:${JSON.stringify(result)}`,
        );
      } else {
        logger.info(
          `✅ stop miloco-backend, details:${JSON.stringify(result)}`,
        );
      }
    },
  };
};
