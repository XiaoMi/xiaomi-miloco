import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import {
  kPluginDescription,
  kPluginId,
  kPluginName,
  MilocoPluginConfigSchema,
} from "./config.js";
import { registerHomeProfile } from "./home-profile/index.js";
import { registerHooks } from "./hooks/index.js";
import { loadSharedConfig } from "./miloco/config.js";
import { registerServices } from "./services/index.js";
import { registerNotifyTool } from "./tools/notify.js";
import { logger } from "./utils/logger.js";
import { registerHttpRoutes } from "./webhooks/index.js";

export default {
  id: kPluginId,
  name: kPluginName,
  description: kPluginDescription,
  configSchema: MilocoPluginConfigSchema,
  register(api: OpenClawPluginApi) {
    logger.init(api);

    // 必须在 registerServices 之前：loadSharedConfig 的副作用是把 gateway 当前
    // token 解析并写入 ~/.openclaw/miloco/config.json::agent.auth_bearer。否则
    // 紧随其后的 backend 拉起读到空 bearer，调回 /miloco/webhook 会 401。
    // 看似不用返回值，实际依赖这个写盘副作用——别再删了。
    loadSharedConfig(api);

    // 注册相关服务和扩展
    registerServices(api);
    registerHooks(api);
    registerHttpRoutes(api);
    registerHomeProfile(api);
    registerNotifyTool(api);
  },
};
