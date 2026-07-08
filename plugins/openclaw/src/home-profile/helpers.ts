import path from "node:path";
import { milocoHome } from "../miloco/paths.js";
import { readTextFileSync } from "../utils/io.js";

/**
 * 习惯建议候选库路径：`$MILOCO_HOME/home-profile/task-suggestions.json`
 *
 * 与 Python 端管理的 profile.json / candidates.json / profile.md / .lock 同目录但文件名独立，互不干扰。
 */
export function habitSuggestionsPath(): string {
  return path.join(milocoHome(), "home-profile", "task-suggestions.json");
}

export function readFileSafe(filePath: string): string {
  try {
    return readTextFileSync(filePath);
  } catch {
    return "";
  }
}
