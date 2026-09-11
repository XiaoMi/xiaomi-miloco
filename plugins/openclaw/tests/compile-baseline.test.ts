import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// build.openclawVersion 是 notify.ts 里宿主镜像实现的论证基线，而 devDependency 是
// 浮动下限——这里把声明钉到实际安装的版本上：任何升级（改 lockfile / devDep）都会
// 在此变红，逼着同步该字段与 notify.ts 的镜像论证，而不是靠知识库里的手动同步义务。
describe("openclaw 编译基线", () => {
  it("build.openclawVersion 与 node_modules 实际安装的 openclaw 版本一致", () => {
    const pkg = JSON.parse(
      readFileSync(new URL("../package.json", import.meta.url), "utf8"),
    ) as { openclaw?: { build?: { openclawVersion?: string } } };
    const installed = JSON.parse(
      readFileSync(
        new URL("../node_modules/openclaw/package.json", import.meta.url),
        "utf8",
      ),
    ) as { version?: string };
    expect(
      pkg.openclaw?.build?.openclawVersion,
      "package.json#openclaw.build.openclawVersion 未随 openclaw 升级同步——" +
        "notify.ts 的宿主镜像实现（默认 agent 解析等）论证基线已漂移，" +
        "请按新版本核对镜像语义并同步该字段与 docstring",
    ).toBe(installed.version);
  });
});
