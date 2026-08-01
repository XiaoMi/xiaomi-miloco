/**
 * 卡片引用的每个文案 key 都必须在两份语言文件里存在。
 *
 * 缺一个不会让类型检查、单测或构建报错 —— 界面上直接显示 `perceptionBackend.tokenLabel`
 * 这样的原始 key,只有真正看一眼页面才发现。这条测试把"看一眼"变成自动的。
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const root = fileURLToPath(new URL("../src/", import.meta.url));

function usedKeys(): string[] {
  const src = readFileSync(`${root}components/PerceptionBackendCard.tsx`, "utf8");
  return [...src.matchAll(/t\("perceptionBackend\.([A-Za-z0-9_]+)"\)/g)].map((m) => m[1]);
}

function definedKeys(locale: string): Set<string> {
  const json = JSON.parse(
    readFileSync(`${root}i18n/locales/${locale}/usage.json`, "utf8"),
  );
  return new Set(Object.keys(json.perceptionBackend ?? {}));
}

describe("感知后端卡片的文案", () => {
  it("组件里确实引用了文案(正则失效时要能发现)", () => {
    expect(usedKeys().length).toBeGreaterThan(5);
  });

  it.each(["zh", "en"])("%s 文案齐全", (locale) => {
    const defined = definedKeys(locale);
    const missing = usedKeys().filter((k) => !defined.has(k));
    expect(missing).toEqual([]);
  });

  it("两份语言文件的 key 集合一致", () => {
    const zh = [...definedKeys("zh")].sort();
    const en = [...definedKeys("en")].sort();
    expect(zh).toEqual(en);
  });
});
