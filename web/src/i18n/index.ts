/**
 * i18n 初始化 —— main.tsx 在 render 前 side-effect 导入。
 *
 * 语言态由 i18next 自身管理,偏好持久化到 localStorage["web:lang"](与 web:theme
 * 对齐)。**没存过偏好时跟随系统/浏览器语言**——中文系统中文、其它一律英文(App 内置
 * 窗口同理)。URL 上的 ?lang=zh|en 是启动器在强制语言时加的,优先级最高。
 * 缺词回退到中文(zh/en 键位由 tests/i18n.test.ts 兜住对齐,正常不会用到回退)。
 *
 * 非组件模块(api/real.ts、lib/relativeTime.ts)直接 `import i18n from "@/i18n"`
 * 用 i18n.t(...) / i18n.language —— i18next 的 t 在 React 组件外同样可用。
 */
import i18n from "i18next";
import { initReactI18next } from "react-i18next";

// 译文按域拆分到 locales/{zh,en}/*.json,用 Vite glob 自动合并 —— 新增域文件
// 无需改本文件。每个域文件形如 { "<域>": { ...keys } },顶层 key 互不重叠。
function mergeLocale(mods: Record<string, unknown>): Record<string, unknown> {
  return Object.assign(
    {},
    ...Object.values(mods).map((m) => (m as { default: unknown }).default ?? m),
  );
}
const zh = mergeLocale(
  import.meta.glob("./locales/zh/*.json", { eager: true }),
);
const en = mergeLocale(
  import.meta.glob("./locales/en/*.json", { eager: true }),
);

export const LANG_KEY = "web:lang";
export type Lang = "zh" | "en";

/** 导出仅为单测覆盖语言解析矩阵(见 tests/langDetect.test.ts)。 */
export function readLang(): Lang {
  // 1) 地址上带语言是原生端(启动器菜单 / MILOCO_APP_LANG)的指定:
  //    - ?lang=zh|en : 直接用
  //    - ?lang=auto  : 原生端选了「跟随系统」,顺手清掉页面里存过的偏好,
  //                    否则页面会把自己存的语言回报给原生,把「跟随系统」顶回去
  if (typeof location !== "undefined") {
    const q = new URLSearchParams(location.search).get("lang");
    if (q === "en" || q === "zh") return q;
    if (q === "auto" && typeof localStorage !== "undefined") {
      localStorage.removeItem(LANG_KEY);
    }
  }
  // 2) 测试(node 环境)给的是 window 桩、没有 localStorage——用 typeof 守住,
  //    缺失时回退中文(测试默认走 zh 路径)。
  if (typeof localStorage === "undefined") return "zh";
  const saved = localStorage.getItem(LANG_KEY);
  if (saved === "en" || saved === "zh") return saved;
  // 3) 没存过偏好:跟随系统/浏览器语言,非中文一律英文(默认英文)。
  const nav =
    typeof navigator !== "undefined"
      ? navigator.language || navigator.languages?.[0] || ""
      : "";
  return nav.toLowerCase().startsWith("zh") ? "zh" : "en";
}

i18n.use(initReactI18next).init({
  resources: {
    zh: { translation: zh },
    en: { translation: en },
  },
  lng: readLang(),
  fallbackLng: "zh",
  interpolation: { escapeValue: false },
});

// 把当前语言回报给原生 App（只有在 App 内置窗口里才有这座桥）——这样在页面里
// 切语言，原生菜单也会跟着切；反过来原生菜单切语言时会带 ?lang= 让页面同步。
function reportLangToNative(lng: string) {
  if (typeof window === "undefined") return;
  const bridge = (
    window as unknown as {
      webkit?: { messageHandlers?: { milocoLang?: { postMessage: (v: string) => void } } };
    }
  ).webkit?.messageHandlers?.milocoLang;
  bridge?.postMessage(lng === "en" ? "en" : "zh");
}

// 切语言 → 持久化 + 同步 <html lang> 与 <title>。初次也设一次。
// index.html 里的 <title> 是静态中文，标签页/浏览器窗口标题得跟着语言走。
function syncHtmlLang(lng: string) {
  if (typeof document !== "undefined") {
    document.documentElement.lang = lng === "en" ? "en" : "zh-CN";
    document.title = i18n.t("lang.pageTitle");
  }
  reportLangToNative(lng);
}

/** 把语言同步给**后端**：感知 system prompt 里的"输出语言"要跟随住户看到的界面语言，
 *  而后端不知道网页选了 zh 还是 en（那是 i18next/localStorage 的事）。写
 *  perception.engine.output_language（热读，下个感知窗口生效）。
 *
 *  刻意**不**挂在 languageChanged 上：那是 import 期就会触发一次的副作用，会让每个
 *  import 过 i18n 的单测都多打一条 PUT（api 契约测试用 fetch 捕获断言"只发了我这一条"，
 *  会被这条污染）。改为两个显式时点：① 用户在页面里切语言（setLanguagePreference）；
 *  ② App 启动（main.tsx 调 syncLanguageToBackend）——覆盖"跟随系统/原生指定"的初始语言。
 *  fire-and-forget：老后端没这个字段 / 接口一时不可用都只该让后端继续用 auto 推断，
 *  绝不能影响"切语言"本身，故吞掉异常（动态 import 见 lib/perceptionOutput 的说明）。 */
export function syncLanguageToBackend(lng: string = i18n.language): void {
  void import("@/lib/perceptionOutput")
    .then((m) => m.syncOutputLanguageToBackend(lng))
    .catch(() => {});
}
syncHtmlLang(i18n.language);
i18n.on("languageChanged", (lng) => syncHtmlLang(lng));

/** 用户在页面里主动切语言：只在这里落盘。
 *
 * 不能放在 languageChanged 回调里落盘：地址上的 ?lang= 是原生端（菜单/环境变量）
 * 指定的语言，i18next 初始化时也会触发一次 languageChanged，于是「原生指定」会被
 * 固化成「页面偏好」——之后原生菜单选「跟随系统」就再也跟不动系统了。 */
export function setLanguagePreference(lng: Lang): Promise<unknown> {
  if (typeof localStorage !== "undefined") localStorage.setItem(LANG_KEY, lng);
  // 用户主动切语言：把新语言同步给后端（感知判定理由的输出语言随界面走）。
  syncLanguageToBackend(lng);
  return i18n.changeLanguage(lng);
}

/** 是否跑在 App 的内置窗口里（原生注册了 milocoLang 通道）。 */
export function inNativeApp(): boolean {
  if (typeof window === "undefined") return false;
  return Boolean(
    (window as unknown as { webkit?: { messageHandlers?: Record<string, unknown> } }).webkit
      ?.messageHandlers?.milocoLang,
  );
}

export default i18n;
