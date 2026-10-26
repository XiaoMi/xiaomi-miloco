/**
 * 实时画面帧率选择。
 *
 * 帧率越高，后端每路都要多解码、多重新编码，低配主机（如 4 核赛扬）开 4 路时
 * 3~5 fps 已接近上限；后端还会按 camera.live_max_fps 钳位，处理不过来时自动降帧。
 * 选择只存浏览器本地，经 iframe 的 ?fps= 传给 watch 页。
 *
 * 选项同时是后端自动降帧/恢复的档位（backend miloco/miot/ws.py 的 LIVE_FPS_STEPS），
 * 两边需保持一致：降帧时逐级下移，恢复时逐级上移，不会出现选项以外的帧率。
 */
export const LIVE_FPS_OPTIONS = [1, 3, 5, 10, 20] as const;
export const DEFAULT_LIVE_FPS = 5;
export const LIVE_FPS_KEY = "web:liveFps";

export function normalizeLiveFps(value: unknown): number {
  const n = typeof value === "number" ? value : parseInt(String(value ?? ""), 10);
  return (LIVE_FPS_OPTIONS as readonly number[]).includes(n) ? n : DEFAULT_LIVE_FPS;
}

export function readLiveFps(): number {
  try {
    return normalizeLiveFps(localStorage.getItem(LIVE_FPS_KEY));
  } catch {
    return DEFAULT_LIVE_FPS; // localStorage 不可用（隐私模式/测试桩）
  }
}

export function saveLiveFps(fps: number): void {
  try {
    localStorage.setItem(LIVE_FPS_KEY, String(normalizeLiveFps(fps)));
  } catch {
    /* 写不了就只在本会话生效 */
  }
}
