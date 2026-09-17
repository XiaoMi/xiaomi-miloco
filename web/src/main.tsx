import React from "react";
import ReactDOM from "react-dom/client";
import { syncLanguageToBackend } from "./i18n";
import "./i18n";
import { App } from "./App";
import { getEdition } from "./api";
import { setEditionInfo } from "./lib/edition";
import "./styles/theme.css";

// 主题决议优先级：URL ?theme= > localStorage > 跟随系统（不设 data-theme）。
// URL ?theme=dark / ?theme=light 是**临时覆盖**（截图 / 工程调试 / 临时看一眼），
// **不写** localStorage——避免"用 URL 看一眼就被永久保存"。
// 只有用户在设置抽屉里主动选的才进 localStorage 持久化。
const themeParam = new URLSearchParams(window.location.search).get("theme");
const savedTheme = localStorage.getItem("web:theme");
if (themeParam === "dark" || themeParam === "light") {
  document.documentElement.setAttribute("data-theme", themeParam);
  const url = new URL(window.location.href);
  url.searchParams.delete("theme");
  window.history.replaceState({}, "", url.toString());
} else if (savedTheme === "dark" || savedTheme === "light") {
  document.documentElement.setAttribute("data-theme", savedTheme);
}

// 启动即把当前界面语言同步给后端（感知 system prompt 的"输出语言"要跟住户看到的一致）。
// 放这里而不是 i18n 模块里：那是 import 期副作用，会把每个 import 过 i18n 的单测都带上
// 一条多余的 PUT（api 契约测试会因此变红）。fire-and-forget，失败只当后端继续用 auto。
syncLanguageToBackend();

function render() {
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}

// 发行版本（full/slim）决定导航与功能入口，先取一次再 render，避免首帧闪出一排
// 马上要消失的 tab。3s 超时 + 失败回退 full：绝不因为老后端 / 一次网络抖动砍功能。
const editionReady = Promise.race([
  getEdition()
    .then(setEditionInfo)
    .catch(() => setEditionInfo(null)),
  new Promise<void>((resolve) => window.setTimeout(resolve, 3000)),
]);
void editionReady.finally(render);
