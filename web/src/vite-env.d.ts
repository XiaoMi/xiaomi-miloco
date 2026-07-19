/// <reference types="vite/client" />

// 由 vite.config.ts 的 define 注入：构建版本号
//（build 用 MILOCO_APP_VERSION 环境变量，缺省兜底 package.json；dev 用 git describe）
declare const __APP_VERSION__: string;
