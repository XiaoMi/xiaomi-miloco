import { beforeEach, describe, expect, it, vi } from "vitest";

const { runShellMock } = vi.hoisted(() => ({ runShellMock: vi.fn() }));

vi.mock("../src/utils/shell.js", () => ({ runShell: runShellMock }));
vi.mock("../src/utils/logger.js", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { createBackendService } from "../src/services/backend.js";
import { logger } from "../src/utils/logger.js";

const kOk = { status: 0, stdout: "{}", stderr: "", signal: null, error: null };
const kFail = {
  status: 1,
  stdout: "",
  stderr: "boom",
  signal: null,
  error: null,
};

/** 宿主 ctx 只在这里用到 serviceHealth；其余字段与用例无关，故按 unknown 收。 */
interface Service {
  id: string;
  start: (ctx: unknown) => void | Promise<void>;
  stop: (ctx: unknown) => Promise<void>;
}

function makeCtx(withHealth = true) {
  const health = { reportFailure: vi.fn(), clearFailure: vi.fn() };
  return { ctx: withHealth ? { serviceHealth: health } : {}, health };
}

function newService(): Service {
  const service = createBackendService({} as never, {} as never);
  if (!service) throw new Error("createBackendService returned undefined");
  // stop 在宿主类型里是可选的，用例里必须存在
  return service as Service;
}

function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

/**
 * 模拟 Node `spawn(cmd, args, { signal })` 的语义：CLI 一直不结束，直到 signal
 * abort 才以 AbortError 收尾。用来验证 start() 不阻塞、stop() 能在飞中掐断。
 */
function pendingRestart() {
  const d = deferred<typeof kFail>();
  runShellMock.mockImplementationOnce(
    (_cmd: string, _args: string[], opts: { signal: AbortSignal }) =>
      new Promise((resolve) => {
        d.promise.then(resolve);
        opts.signal.addEventListener("abort", () =>
          resolve({
            status: 1,
            stdout: "",
            stderr: "",
            signal: null,
            error: Object.assign(new Error("The operation was aborted"), {
              name: "AbortError",
            }),
          }),
        );
      }),
  );
  return d;
}

const signalOf = (call: number) =>
  runShellMock.mock.calls[call]?.[2]?.signal as AbortSignal;

describe("createBackendService", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("start 不等 CLI 结束就返回（宿主在安装/reload 路径只给 5s）", async () => {
    const d = pendingRestart();
    const { ctx } = makeCtx();

    // CLI 还挂着（d 未 settle），start 必须已经返回
    await newService().start(ctx);

    expect(runShellMock).toHaveBeenCalledWith(
      "miloco-cli",
      ["service", "restart"],
      expect.objectContaining({
        signal: expect.any(AbortSignal),
        killSignal: "SIGKILL",
      }),
    );
    expect(signalOf(0).aborted).toBe(false);
    d.resolve(kFail);
  });

  it("后台成功 → clearFailure", async () => {
    runShellMock.mockResolvedValueOnce(kOk);
    const { ctx, health } = makeCtx();

    newService().start(ctx);

    await vi.waitFor(() => expect(health.clearFailure).toHaveBeenCalled());
    expect(health.reportFailure).not.toHaveBeenCalled();
  });

  it("后台失败 → reportFailure（而不是静默）", async () => {
    runShellMock.mockResolvedValueOnce(kFail);
    const { ctx, health } = makeCtx();

    newService().start(ctx);

    await vi.waitFor(() => expect(health.reportFailure).toHaveBeenCalled());
    expect(health.reportFailure.mock.calls[0][0]).toBeInstanceOf(Error);
    expect(health.clearFailure).not.toHaveBeenCalled();
  });

  it("ctx 上没有 serviceHealth（老宿主）时不抛", async () => {
    runShellMock.mockResolvedValueOnce(kFail);

    newService().start(makeCtx(false).ctx);

    await vi.waitFor(() =>
      expect(logger.error).toHaveBeenCalledWith(
        expect.stringContaining("❌ start miloco-backend"),
      ),
    );
  });

  it("stop 先掐断在飞的 restart，再跑 service stop", async () => {
    const d = pendingRestart();
    const { ctx, health } = makeCtx();
    const service = newService();
    service.start(ctx);

    runShellMock.mockResolvedValueOnce(kOk); // 给 service stop
    await service.stop(ctx);

    expect(signalOf(0).aborted).toBe(true); // 在飞的那条被掐
    expect(runShellMock).toHaveBeenCalledTimes(2);
    expect(runShellMock.mock.calls[1][1]).toEqual(["service", "stop"]);
    // 被掐断的那次不算成败：两个上报都不调
    await vi.waitFor(() =>
      expect(logger.warn).toHaveBeenCalledWith(
        expect.stringContaining("被 stop 掐断"),
      ),
    );
    expect(health.reportFailure).not.toHaveBeenCalled();
    expect(health.clearFailure).not.toHaveBeenCalled();
    d.resolve(kFail);
  });

  it("stop 之后再 start（替换失败后宿主的恢复路径）用新 controller", async () => {
    const d = pendingRestart();
    const { ctx, health } = makeCtx();
    const service = newService();
    service.start(ctx);

    runShellMock.mockResolvedValueOnce(kOk);
    await service.stop(ctx);
    expect(signalOf(0).aborted).toBe(true);

    runShellMock.mockResolvedValueOnce(kOk);
    service.start(ctx);

    const first = signalOf(0);
    const second = signalOf(2);
    expect(second).not.toBe(first);
    expect(second.aborted).toBe(false); // 复用过期的 controller 会直接 spawn 失败
    await vi.waitFor(() => expect(health.clearFailure).toHaveBeenCalled());
    d.resolve(kFail);
  });

  it("没 start 过也能 stop（CLI 侧幂等）", async () => {
    runShellMock.mockResolvedValueOnce(kOk);
    const { ctx } = makeCtx();

    await newService().stop(ctx);

    expect(runShellMock).toHaveBeenCalledTimes(1);
    expect(runShellMock.mock.calls[0][1]).toEqual(["service", "stop"]);
  });

  it("上报口自身抛错也不能漏成 unhandled rejection（后台链要有 catch）", async () => {
    runShellMock.mockResolvedValueOnce(kFail);
    const ctx = {
      serviceHealth: {
        reportFailure: () => {
          throw new Error("reporter exploded");
        },
        clearFailure: vi.fn(),
      },
    };

    newService().start(ctx);

    // 回调抛错必须被 catch 接住并落到日志，而不是让 done 变成无人消费的 rejection
    await vi.waitFor(() =>
      expect(logger.error).toHaveBeenCalledWith(
        expect.stringContaining("后台收尾异常"),
      ),
    );
  });
});
