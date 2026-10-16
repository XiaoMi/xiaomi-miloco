import { describe, expect, it } from "vitest";
import { runShell } from "../src/utils/shell.js";

describe("runShell", () => {
  it("异步 spawn 回传 stdout/stderr/code", async () => {
    const result = await runShell("/bin/sh", [
      "-c",
      "echo hello; echo err 1>&2; exit 0",
    ]);
    expect(result.status).toBe(0);
    expect(result.stdout).toContain("hello");
    expect(result.stderr).toContain("err");
  });

  it("非零退出码正确回传", async () => {
    const result = await runShell("/bin/sh", ["-c", "exit 7"]);
    expect(result.status).toBe(7);
  });

  // miloco-backend 的 stop() 靠这个语义掐断在飞的 `service restart`（见 services/backend.ts）
  it("signal abort 杀掉子进程并以 AbortError 收尾", async () => {
    const ac = new AbortController();
    const t0 = Date.now();
    const pending = runShell("/bin/sh", ["-c", "exec sleep 30"], {
      signal: ac.signal,
      killSignal: "SIGKILL",
    });
    setTimeout(() => ac.abort(), 100);
    const result = await pending;

    expect(Date.now() - t0).toBeLessThan(5_000);
    expect(result.error?.name).toBe("AbortError");
    expect(result.stdout).toBe("");
  });

  it("signal 预先已 abort 时不真的 spawn", async () => {
    const ac = new AbortController();
    ac.abort();
    const result = await runShell("/bin/sh", ["-c", "echo should-not-run"], {
      signal: ac.signal,
    });
    expect(result.stdout).toBe("");
    expect(result.error?.name).toBe("AbortError");
  });
});
