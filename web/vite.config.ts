import react from "@vitejs/plugin-react";
import { execSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
// 用 vitest/config 的 defineConfig，让 `test` 字段类型可识别
import { defineConfig } from "vitest/config";

/**
 * Dev-only: 启动时从本机 ~/.openclaw/miloco/config.json 读出 backend 的
 * server.token，proxy 转发 /api & /health 时自动加 Authorization header。
 * 当前默认入口走 backend 1810 SPA handler(README 已声明 vite dev server 退役),
 * `pnpm scripts` 也不暴露 dev mode;本函数 + attachAuth + server.proxy 仅留作
 * 临时 `vite serve`(恢复 _mock 假数据通道时)兜底。
 *
 * 安全：token 只在 vite dev 进程内部使用，不进 HTML、不进浏览器全局变量，
 * 浏览器和前端代码都看不到。生产构建用 backend SPA handler 自带的注入逻辑。
 */
function readBackendToken(): string {
  if (process.env.MILOCO_TOKEN) return process.env.MILOCO_TOKEN;
  const cfgPath = path.join(os.homedir(), ".openclaw", "miloco", "config.json");
  try {
    const raw = fs.readFileSync(cfgPath, "utf-8");
    return JSON.parse(raw)?.server?.token ?? "";
  } catch {
    return "";
  }
}

// 开发期把 /api /health 代理到 backend;env 可覆盖。backend 永远 HTTP(跨网
// 加密走反代+真证书),不再有 self-signed cert 路径。secure:true 是默认。
const BACKEND = process.env.VITE_BACKEND_URL ?? "http://127.0.0.1:1810";

// 注:身份注册路由(/api/identity/*)已迁移到主 backend(miloco.person.router),
// 不再依赖独立的 register_server(8765)。前端直接走 /api/* 即可。

const DEV_TOKEN = readBackendToken();

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function attachAuth(proxy: any) {
  if (!DEV_TOKEN) return;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  proxy.on("proxyReq", (proxyReq: any) => {
    proxyReq.setHeader("Authorization", `Bearer ${DEV_TOKEN}`);
  });
}

// build 时注入界面版本；dev 用 git describe。用 vite 的 command 判别 build/serve——不能用
// process.env.NODE_ENV，vite 执行本配置文件时不会把它设成 "production"，build 下会误走 git 分支。
const __pkgVersion = JSON.parse(
  fs.readFileSync(path.resolve(__dirname, "package.json"), "utf-8"),
).version as string;

function resolveAppVersion(command: string): string {
  if (command === "build") {
    // build.sh 经 MILOCO_APP_VERSION 注入 RESOLVED_PEP(与 CLI/Python 包权威版本同源一致)；
    // 这才是界面该显示的版本。package.json 的 version 只是 npm 装配占位(0.0.0-dev.<sha>)，
    // 仅当脱离 build.sh 直接 `pnpm build` 时兜底用它。
    return process.env.MILOCO_APP_VERSION || __pkgVersion;
  }
  try {
    return execSync("git describe --tags --always --dirty", {
      encoding: "utf-8",
    }).trim();
  } catch {
    return __pkgVersion;
  }
}

export default defineConfig(({ command }) => ({
  define: {
    __APP_VERSION__: JSON.stringify(resolveAppVersion(command)),
  },
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
    host: "0.0.0.0",
    allowedHosts: true,
    proxy: {
      "/api": {
        target: BACKEND,
        changeOrigin: true,
        ws: true,
        configure: attachAuth,
      },
      "/health": {
        target: BACKEND,
        changeOrigin: true,
        configure: attachAuth,
      },
    },
  },
  build: {
    outDir: "dist",
    // 生产 build 不留 .map(即便 spa_handler 真文件分支命中也无源可取);
    // 生成时拿不到 NODE_ENV(vite 默认 production for build),用 process.env
    // 兜底,本地 staging 排查需 source 时手开 `MILOCO_VITE_SOURCEMAP=hidden`。
    sourcemap:
      process.env.MILOCO_VITE_SOURCEMAP === "hidden" ? "hidden" : false,
    target: "es2022",
    rollupOptions: {
      output: {
        // 函数式 manualChunks:把 react/react-dom/scheduler 整套依赖(含
        // ReactDOM ~150KB)都拉进 vendor chunk,业务代码改动不让浏览器重下
        // 整 bundle。string 数组形式只匹配 entry,递归依赖仍在 main chunk。
        // 用 path segment 精确匹配 node_modules/{react,react-dom,scheduler}/,
        // 防 react-router-dom / react-icons 等未来引入的 react-* 包被误吞 vendor。
        manualChunks(id: string) {
          if (
            /[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/.test(id)
          ) {
            return "vendor";
          }
        },
      },
    },
  },
  // vitest config: 测试集中在 tests/(跟 src/ 并列,与 backend 的 tests/ 命名一致),
  // 不 co-located 散在源码目录。测试 import 被测源码走 @/ 别名(见上方 resolve.alias)。
  // 用 node 环境（不依赖 jsdom）：node 18+ 自带 fetch / Response / Headers，
  // localStorage 在 setup 文件里 polyfill；纯函数 + api 层契约测试足够。
  test: {
    environment: "node",
    globals: true,
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
    setupFiles: ["./tests/test-setup.ts"],
  },
}));
