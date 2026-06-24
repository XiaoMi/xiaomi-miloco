# Miloco 家庭面板（web）

> 项目根的 `web/` 子工程——面向家庭住户的 React Web 面板。沿用项目早期预留的 `web/` 目录（取代之前的占位 README）。

面向家庭住户的 Web 面板。**默认直连 miloco backend**——backend 没起就显示真实失败态。`_mock/` 目录的 sessionStorage 假数据通道已**完全删除**(`vite.config.ts::mockAutoInject` plugin 跟 `_mock/register.ts` 都从源码摘掉);如需 UI 调试假数据,从 git 历史拉回 plugin + register.ts 再跑临时 `vite serve`。

## 部署架构

**单端口模型**：backend 永远 HTTP（跨网加密走反代+真证书），住户访问 `http://<host>:1810/` 直接拿到 SPA。`vite build` 把产物写到 `../backend/miloco/src/miloco/static/`，backend `spa_handler` 路由：真文件命中（如 `/assets/*.js`、`/fonts/*.woff2`）→ `FileResponse`；根 `/` 与 `/index.html` → SPA `index.html` + 把 `__MILOCO_INJECT_TOKEN_HERE__` 占位替换成真 `server.token`（浏览器从 `window.__MILOCO_TOKEN__` 读出加 Authorization Bearer）；其它非根路径未命中真文件 → 404（避免扫描器 `/admin/login`、`/.env` 等都拿 token-injected HTML）。

旧的 vite dev server 5173 + proxy::attachAuth 模式已退役，**没有 `pnpm dev`**——开发期照样跑 `pnpm build:watch` 让产物落地，浏览器直开 `http://<host>:1810/`。

## 启动

backend 任选一种：

```bash
# 方式 A：cli 后台 daemon（推荐）
miloco-cli service start
miloco-cli service status     # 看活没活
miloco-cli service logs -f    # 跟日志
miloco-cli service stop       # 关

# 方式 B：直接前台跑（看 stdout 方便，关终端就没）
cd ../backend && uv run miloco-backend
```

前端：

```bash
pnpm install              # 首次
pnpm build                # 产物落到 ../backend/miloco/src/miloco/static/（含 tsc -b 类型检查）
# 或开发期 watch 模式（**只跑 vite，不跑 tsc -b**——type 错不会阻止重建。
# 需要类型检查时另开窗口跑 `pnpm typecheck`）
pnpm build:watch     # 改文件自动重建，浏览器手刷
```

**身份注册服务**：已迁移到主 backend——前端「让它认识 X」走 `/api/identity/persons/{id}/extract`
（图片 / 视频 multipart）+ `/api/identity/persons/{id}/samples/batch`（JSON 批量落样本），
跟主 backend 同进程同 token，**无需手动启额外服务**。

历史上自动抽帧能力由独立的离线注册服务(8765 端口)提供、需手跑,现已退役并移除——前端走主
backend `/api/identity/persons` 同源相对路径,LAN 手机 / 平板访问家庭面板录家人功能正常可用。

**浏览器**：直接打开 `http://<host>:1810/`（本机 = `http://127.0.0.1:1810/`，
LAN = `http://192.168.x.x:1810/`）。SPA 启动时从注入的 `window.__MILOCO_TOKEN__`
读 token，所有 fetch 自动带 Authorization Bearer。

> **LAN 访问**：默认仅绑定 `127.0.0.1`，局域网其它设备无法访问。
> 需在 `~/.openclaw/miloco/config.json` 中将 `host` 改为 `"0.0.0.0"` 并重启服务：
> ```json
> { "server": { "host": "0.0.0.0" } }
> ```
> 注意：开启后 token 将对 LAN 可见，请确认局域网可信（私网 + 单管理员 OK，
> 共享网络 / 路由器穿透应走反代 + TLS + 认证）。详见 `settings.py` 中
> `ServerSettings.host` 的说明。

## 其它命令

```bash
pnpm build       # 构建产物 → ../backend/miloco/src/miloco/static/
pnpm typecheck   # 仅类型检查
pnpm test        # vitest 单测
```

## 数据源

默认直连 backend。`_mock/` 假数据通道已删,**仅当**需要恢复 mock 调试时才手动从 git 历史拉回 plugin + register.ts + 跑临时 `vite serve`(当前 pnpm scripts 不暴露,跟 README §部署架构 dev server 退役声明对齐)。生产构建始终直连 backend:

- 所有 API 调用都走 `src/api/real.ts` 包装的 `apiFetch` → backend HTTP
- mock 模式整套已删(plugin / _mock/register.ts / MOCK 检测,`src/api/index.ts` 直接 `impl = realImpl`);如需短期恢复,从 git 历史把 mockAutoInject 插件 + register.ts 拉回再临时跑 `vite serve`
- 失败抛 `ApiError`，由 `useAsync` 收掉，UI 上 toast / 空态提示
- `src/api/index.ts` 是统一出口

仍然用占位的少数能力（backend 还没接口暴露）以代码内 TODO 注释为准。

## Token 解析

backend `spa_handler` 把 `index.html` 里的 `__MILOCO_INJECT_TOKEN_HERE__` 字符串替换成 `json.dumps(server.token)[1:-1]`（JS-string-escape，处理 token 含双引号 / 反斜杠等特殊字符的边界）。前端 `src/api/client.ts::resolveToken` 读 `window.__MILOCO_TOKEN__`，未注入时占位仍在 → 退化为空串语义。

## 视觉对齐

**v3 Mi Console** 极客控制台风：参考 [mi.com/global/support](https://www.mi.com/global/support/) 的克制专业 × Stripe Dashboard / Vercel / Linear 的 dev tool 密度感。

- 白底 + 小米橙 `#FF6700` 作功能色（链接/active/CTA），不作面色
- 中文字体走系统通用栈（MiSans → 苹方 → 雅黑），不依赖 CDN
- 数字 / 英文 / IID / device_id / task_id 用 **Geist Mono**
- light 主线 + dark 支持（顶栏 ☀ / 🌙 切；设置抽屉里有 auto/light/dark 三档）
- 状态信息用 5px 实心点 + 半透明光环（不再用大色块 chip）

视觉契约参考：`knowledge/07-design/`（设计 SSOT，6 篇规范文档）。
