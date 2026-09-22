/**
 * i18n 资源不变量 —— 守两类静默 bug：
 *  1. zh/en key 不对齐：en 缺 key 会被 fallbackLng 回退成中文，英文模式露中文。
 *  2. 插值占位符不一致：zh 用 {{msg}}、en 误写 {{message}}，运行期插值静默失效。
 * 外加 i18n 接线 smoke：默认 zh、切 en 生效、能切回。
 */
import { describe, it, expect, afterAll } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import i18n from "@/i18n";

const localesDir = fileURLToPath(new URL("../src/i18n/locales/", import.meta.url));

function flat(obj: Record<string, unknown>, prefix = ""): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(obj)) {
    const nk = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object") Object.assign(out, flat(v as Record<string, unknown>, nk));
    else out[nk] = String(v);
  }
  return out;
}

function loadDomain(loc: "zh" | "en", file: string): Record<string, string> {
  return flat(JSON.parse(readFileSync(`${localesDir}${loc}/${file}`, "utf8")));
}

function placeholders(s: string): string[] {
  return [...s.matchAll(/\{\{\s*(\w+)\s*\}\}/g)].map((m) => m[1]).sort();
}

const domains = readdirSync(`${localesDir}zh`).filter((f) => f.endsWith(".json"));

describe("i18n 资源完整性", () => {
  it("zh/en 域文件一一对应", () => {
    expect(readdirSync(`${localesDir}en`).filter((f) => f.endsWith(".json")).sort()).toEqual(
      [...domains].sort(),
    );
  });

  describe.each(domains)("%s", (file) => {
    const zh = loadDomain("zh", file);
    const en = loadDomain("en", file);

    it("zh/en key 完全对齐(防 en 缺 key 回退露中文)", () => {
      expect(Object.keys(en).sort()).toEqual(Object.keys(zh).sort());
    });

    // time 域有意例外：日期里中文用 {{m}}(数字月)、英文用 {{mon}}(短月名),
    // relativeTime 同时传 m+mon 由各语言模板各取所需,故占位符不要求两语言一致。
    const SKIP_PLACEHOLDER = new Set(["time.json"]);
    it.skipIf(SKIP_PLACEHOLDER.has(file))(
      "每个 key 的插值占位符 {{x}} 两语言一致",
      () => {
        for (const k of Object.keys(zh)) {
          expect(placeholders(en[k] ?? "")).toEqual(placeholders(zh[k]));
        }
      },
    );
  });
});

describe("i18n 接线 smoke", () => {
  afterAll(async () => {
    await i18n.changeLanguage("zh");
  });

  it("默认 zh,glob 合并后取词正常", () => {
    expect(i18n.language).toBe("zh");
    expect(i18n.t("nav.home")).toBe("概览");
  });

  it("切 en 后取到英文", async () => {
    await i18n.changeLanguage("en");
    expect(i18n.t("nav.home")).toBe("Overview");
  });

  it("未知 key 回退为 key 本身(不抛错)", () => {
    expect(i18n.t("nav.__missing__")).toBe("nav.__missing__");
  });
});

// 守 USAGE 卡:组件里引用的 usage.* key(含 t("usage.X") 与 OMNI_CODE_KEY 的 "usage.X" 值)
// 必须在 zh/en 都存在。这正是「删除弹窗渲染裸 key」事故的根因门——缺 key 时 i18next 回退为
// key 字符串本身,界面显示 "usage.deleteDialogTitle" 这种,既有对齐测试查不出(两边都缺)。
describe("UsageOmniConfig 引用的 usage.* key 均存在", () => {
  const componentPath = fileURLToPath(
    new URL("../src/components/UsageOmniConfig.tsx", import.meta.url),
  );
  const src = readFileSync(componentPath, "utf8");
  const referenced = [...new Set([...src.matchAll(/usage\.([a-zA-Z0-9_]+)/g)].map((m) => m[1]))];
  // loadDomain 把 {usage:{...}} 展平为 "usage.X" 键,故用全名做集合成员判断。
  const zhKeys = new Set(Object.keys(loadDomain("zh", "usage.json")));
  const enKeys = new Set(Object.keys(loadDomain("en", "usage.json")));

  it("至少扫到一批 key(防正则失效后静默放行)", () => {
    expect(referenced.length).toBeGreaterThan(10);
  });

  it.each(referenced)("usage.%s 在 zh 与 en 均有定义", (key) => {
    expect(zhKeys.has(`usage.${key}`), `zh 缺 usage.${key}`).toBe(true);
    expect(enKeys.has(`usage.${key}`), `en 缺 usage.${key}`).toBe(true);
  });
});

/**
 * 日志页(事件流 + 动作流 + 折叠件)引用的 actions.* key 均存在。
 *
 * 与上面那段同法,只是被扫的是日志页那几处源文件——**组件与 lib 都要算**:折叠的装配
 * 与文案拼装有一半在 lib 里,只扫组件会漏掉那半,而漏掉的那半恰恰没有别的东西把守:
 * 格式化延迟那组用例喂的是假 t、断言的是拼装结果,把单位键从 zh/en 一起删掉也不变红,
 * 运行时却会原样渲染出键名。加它的理由:zh/en 对齐那段只比对两侧的**键集合**——两边
 * 都缺同一个键时它照样绿,而界面会原样显示 `actions.badgeAria`。
 * 组件的静态渲染测试能盖住渲染路径上取到的键,取不到的(查询失败、范围外 chip 这些
 * 边角分支)只能靠扫源码。
 *
 * 只认带引号的字面量:`actions.push` 这种成员访问不是 key。源文件里的 key 全是
 * 字面量(没有模板串拼 key),漏网的拼法会在下面那条「至少扫到一批」上先露馅。
 */
describe("日志页引用的 actions.* key 均存在", () => {
  const SOURCES = [
    "../src/components/ActivityFeed.tsx",
    "../src/components/ActionsFeed.tsx",
    "../src/components/FeedFold.tsx",
    "../src/lib/actionText.ts",
    "../src/lib/feedFold.ts",
  ].map((f) => readFileSync(fileURLToPath(new URL(f, import.meta.url)), "utf8"));
  const referenced = [
    ...new Set(
      SOURCES.flatMap((src) =>
        [...src.matchAll(/["'`]actions\.([a-zA-Z0-9_]+)["'`]/g)].map((m) => `actions.${m[1]}`),
      ),
    ),
  ];
  const zhKeys = new Set(Object.keys(loadDomain("zh", "actions.json")));
  const enKeys = new Set(Object.keys(loadDomain("en", "actions.json")));

  it("至少扫到一批 key(防正则失效后静默放行)", () => {
    expect(referenced.length).toBeGreaterThan(20);
  });

  it.each(referenced)("%s 在 zh 与 en 均有定义", (key) => {
    expect(zhKeys.has(key), `zh 缺 ${key}`).toBe(true);
    expect(enKeys.has(key), `en 缺 ${key}`).toBe(true);
  });
});
