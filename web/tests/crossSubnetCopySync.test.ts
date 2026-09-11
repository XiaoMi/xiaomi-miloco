/**
 * 跨网段拉流失败文案的两份前端副本必须字字相同。
 *
 * 同一个故障有两个入口，各查自己的本地译文表：
 *  - 播放页 watch.html 的内联 i18n 表（key: camUnreachableCrossSubnet），
 *    由后端首帧看门狗的 reason `camera_unreachable_cross_subnet` 触发；
 *  - React「家里此刻」卡片的 hero.json（key: streamErrorCrossSubnetNat），
 *    由列表接口的 `stream_error=cross_subnet_nat` 触发。
 *
 * 两份是独立维护的副本，将来改措辞漏改一处，播放页与卡片会对同一故障给出不同说法，
 * 而分裂是静默的——后端那份 fallback message 有单测钉住，前端两份原本没有。
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const heroPath = (loc: "zh" | "en") =>
  fileURLToPath(new URL(`../src/i18n/locales/${loc}/hero.json`, import.meta.url));
const watchHtml = readFileSync(
  fileURLToPath(new URL("../public/watch.html", import.meta.url)),
  "utf8",
);

/** watch.html 里 zh/en 两张表按出现顺序各有一条同名 key，取第 n 条的字符串值。 */
function watchCopies(key: string): string[] {
  const re = new RegExp(`${key}:\\s*"((?:[^"\\\\]|\\\\.)*)"`, "g");
  return [...watchHtml.matchAll(re)].map((m) => JSON.parse(`"${m[1]}"`));
}

function heroCopy(loc: "zh" | "en", key: string): string {
  const json = JSON.parse(readFileSync(heroPath(loc), "utf8")) as {
    hero: Record<string, string>;
  };
  return json.hero[key];
}

describe("cross-subnet 文案副本同步", () => {
  it("watch.html 恰好有 zh/en 两份副本", () => {
    expect(watchCopies("camUnreachableCrossSubnet")).toHaveLength(2);
  });

  it("zh 两份副本一致", () => {
    const [zh] = watchCopies("camUnreachableCrossSubnet");
    expect(zh).toBe(heroCopy("zh", "streamErrorCrossSubnetNat"));
  });

  it("en 两份副本一致", () => {
    const [, en] = watchCopies("camUnreachableCrossSubnet");
    expect(en).toBe(heroCopy("en", "streamErrorCrossSubnetNat"));
  });
});
