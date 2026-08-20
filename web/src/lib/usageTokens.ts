/**
 * 用量 token 的口径工具。放在这里而不是塞进计价模块，是因为它属于**数据口径**：
 * 时间序列分桶要用它，费用估算也要用它，而计价不该被数据层反向依赖。
 */

/**
 * 「文本」项的口径：input 减掉 video 与 audio 的**残差**，夹到 0。
 *
 * 单独抽出来是因为这条规则是全流程最容易搞错的一处：后端的 input 已经含
 * video + audio（还含图片——未单列该模态），拿 input 再乘一次文本价会把
 * video / audio 收两遍。分桶与计价必须走同一个定义。
 *
 * 也因此它不叫「纯文本」：残差里还有图片与系统提示。
 *
 * 夹 0 是防上游偶发不自洽（video + audio > input），不是为了掩盖它。
 */
export function textResidual(input: number, video: number, audio: number): number {
  return Math.max(input - video - audio, 0);
}
