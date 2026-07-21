import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { registerHomeProfileScheduler } from "./scheduler.js";

export function registerHomeProfile(api: OpenClawPluginApi): void {
  registerHomeProfileScheduler(api);
}
