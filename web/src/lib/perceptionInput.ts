/**
 * 「设置 → 感知输入」的默认值与 payload 组装（纯逻辑，无 React/DOM 依赖）。
 *
 * 单独成文件是为了能在 node 环境的单测里直接引用：测试引用这里的常量，SettingsDrawer
 * 也引用同一个常量，两边不可能各写一个字面量后悄悄漂移。
 *
 * 默认值必须与后端 settings.yaml 一致（rule_only_input=image / last_frame_only=false，
 * 即"图片输入 + 每窗多帧"）。
 */

/** 感知输入的两种模式，顺序即 UI 按钮顺序。 */
export const PERCEPTION_INPUT_MODES = ["image", "video"] as const;
export type PerceptionInputMode = (typeof PERCEPTION_INPUT_MODES)[number];

/** 默认：图片输入（不是视频）。 */
export const DEFAULT_PERCEPTION_INPUT: PerceptionInputMode = "image";
/** 默认：一窗多帧全发（不是只送末帧；动作/手势类规则更稳）。 */
export const DEFAULT_IMAGE_LAST_FRAME_ONLY = false;

/** 把非法/缺失的远端值收敛成合法模式（老后端不返该字段时回退默认）。 */
export function normalizePerceptionInput(raw: unknown): PerceptionInputMode {
  return raw === "video" ? "video" : DEFAULT_PERCEPTION_INPUT;
}

/** 两个开关的 PUT payload。字段名必须与后端 PerceptionConfigBody 一致：
 *  `perception_input` / `image_last_frame_only` —— 写错会被 pydantic 静默忽略，
 *  表现为"保存成功但没生效"。 */
export function buildPerceptionInputPayload(
  mode: PerceptionInputMode,
  lastFrameOnly: boolean,
): { perception_input: PerceptionInputMode; image_last_frame_only: boolean } {
  return { perception_input: mode, image_last_frame_only: lastFrameOnly };
}
