/**
 * 语言解析矩阵 —— 界面语言「默认英文、跟随系统」这条链路的单测。
 *
 * 覆盖 readLang() 的四类输入：
 *   1. ?lang=zh|en  （原生端菜单 / MILOCO_APP_LANG 指定）
 *   2. ?lang=auto   （原生端选了「跟随系统」→ 清掉页面存过的偏好再按系统来）
 *   3. localStorage 里存过的偏好
 *   4. 都没有 → 跟随 navigator.language，非中文一律英文
 *
 * node 测试环境默认没有 location/localStorage/navigator，这里用 vi.stubGlobal
 * 造出浏览器侧的三件套。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import i18n, { inNativeApp, readLang, setLanguagePreference, LANG_KEY } from "@/i18n";

type Store = Map<string, string>;

function stubBrowser(opts: { search?: string; stored?: string; nav?: string }): Store {
  const store: Store = new Map();
  if (opts.stored !== undefined) store.set(LANG_KEY, opts.stored);
  vi.stubGlobal("location", { search: opts.search ?? "" });
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  });
  vi.stubGlobal("navigator", { language: opts.nav ?? "en-US" });
  return store;
}

afterEach(() => vi.unstubAllGlobals());

describe("readLang", () => {
  it("?lang=en / ?lang=zh 优先于页面里存过的偏好", () => {
    stubBrowser({ search: "?lang=en", stored: "zh" });
    expect(readLang()).toBe("en");
    stubBrowser({ search: "?lang=zh", stored: "en" });
    expect(readLang()).toBe("zh");
  });

  it("?lang=auto 会清掉页面偏好，之后按系统语言解析", () => {
    const store = stubBrowser({ search: "?lang=auto", stored: "en", nav: "zh-Hans-CN" });
    expect(readLang()).toBe("zh");
    expect(store.has(LANG_KEY)).toBe(false);
  });

  it("存过的偏好优先于系统语言", () => {
    stubBrowser({ stored: "en", nav: "zh-Hans-CN" });
    expect(readLang()).toBe("en");
    stubBrowser({ stored: "zh", nav: "en-US" });
    expect(readLang()).toBe("zh");
  });

  it("setLanguagePreference：只有页面里主动选语言才落盘（原生指定不落盘）", async () => {
    const store = stubBrowser({ nav: "zh-Hans-CN" });
    // 地址上是原生指定的语言时，readLang 不写偏好
    expect(readLang()).toBe("zh");
    expect(store.has(LANG_KEY)).toBe(false);
    // 用户在页面里主动选，才写
    await setLanguagePreference("en");
    expect(store.get(LANG_KEY)).toBe("en");
    expect(i18n.language).toBe("en");
    await i18n.changeLanguage("zh");
  });

  it("inNativeApp：只有在 App 内置窗口（有原生桥）里才为真", () => {
    stubBrowser({});
    expect(inNativeApp()).toBe(false);
    vi.stubGlobal("window", {
      webkit: { messageHandlers: { milocoLang: { postMessage: () => undefined } } },
    });
    expect(inNativeApp()).toBe(true);
  });

  it("没有偏好时跟随系统：中文系统→zh，其它→en（默认英文）", () => {
    stubBrowser({ nav: "zh-Hans-CN" });
    expect(readLang()).toBe("zh");
    stubBrowser({ nav: "zh-TW" });
    expect(readLang()).toBe("zh");
    stubBrowser({ nav: "fr-FR" });
    expect(readLang()).toBe("en");
    stubBrowser({ nav: "" });
    expect(readLang()).toBe("en");
  });
});
