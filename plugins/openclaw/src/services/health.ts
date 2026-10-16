/**
 * service 后台启动成败的上报口。
 *
 * 宿主从 openclaw 2026.9.1 起在 service ctx 上暴露 serviceHealth，但仓库钉住的
 * devDependency openclaw@2026.5.20 类型里没有这个字段 —— 直接写 `ctx.serviceHealth`
 * 过不了 `tsc --noEmit`，只能结构化 cast。老宿主上没有它（老宿主也没有安装/reload
 * 路径的 5s 启动截止，不需要这条通道），调用方一律 `?.` 静默降级。
 */
export interface PluginServiceHealth {
  reportFailure: (error: unknown) => void;
  clearFailure: () => void;
}

export function serviceHealth(ctx: unknown): PluginServiceHealth | undefined {
  return (ctx as { serviceHealth?: PluginServiceHealth } | undefined)
    ?.serviceHealth;
}
