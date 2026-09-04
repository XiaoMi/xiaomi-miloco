/**
 * 用量侧 Base URL 的展示形式：明细表的模型列，以及时间分布浮层的来源行。
 *
 * 模型的唯一身份是 **(模型名 + Base URL)**——同一个模型名完全可以同时挂在两个
 * endpoint 上。用量表自 DB schema v3 起记录了 base_url 原文，所以明细行**直接读
 * 记录值，不做任何反查或推断**。
 *
 * 老数据（v3 之前）的 base_url 是空串，展示侧直说「旧版本数据未记录 URL」：来源
 * 确实无从得知，猜一个（比如按当前生效档案）在任何换过 endpoint 的机器上都是错的，
 * 而把断言写进库会让它和记录值长得一模一样、日后再也分不清哪个是量出来的。
 *
 * 于是本模块只剩一件事：把完整 URL 压成能放进一格的短形式，且**保证同屏并列的那组
 * 互不相同**——压完撞车就等于显示了却分辨不出，而那是显示 URL 的全部意义。
 */

/**
 * 把单个 URL 压到 `max` 个字符以内。
 *
 * 两条规则都是量出来的，不是随手定的：
 *
 * 1. **先去掉 scheme**。`https://` 是每行都一样的 8 个字符、零信息量，
 *    先扣掉才有预算显示真正有差别的部分。
 * 2. **省略号放中间，保住主机头与完整尾段**。两个 endpoint 的差异**通常在尾部**
 *    （`/v1` vs `/v1-test`），而头部往往每行相同——直接截尾会把两行截成
 *    **完全一样的字串**（实测：超长路径那组在 14~28 四档全撞，只差尾段那组在 14 与 18 撞），
 *    显示了却分辨不出。主机头则用来认「是哪家」，主机不同时缺了它同样看不出。
 *
 * 尾段本身就快占满预算时（超长路径）退化成纯头部省略——此时保尾比保头重要，
 * 因为差异在尾部。
 *
 * ⚠️ 单独用它**不保证**一组 URL 压完互不相同（差异可能落在预算之外）。
 * 任何要同屏并列多个 URL 的地方都请用 shortenUrlSet。
 */
export function shortenUrl(url: string, max: number): string {
  const s = url.replace(/^https?:\/\//, "");
  if (s.length <= max) return s;
  const slash = s.indexOf("/");
  const tail = slash >= 0 ? s.slice(slash) : "";
  const host = slash >= 0 ? s.slice(0, slash) : s;
  const budget = max - 1 - tail.length; // 1 = 省略号
  // 预算不够给主机头留下有意义的几个字符 → 退化成纯头部省略，保尾
  if (budget <= 3) return "…" + s.slice(-(max - 1));
  return host.slice(0, budget) + "…" + tail;
}

/**
 * 把**一组** URL 各自压短，并保证压完之后**互不相同**。
 *
 * 为什么不能只按固定规则逐个截：差异可能落在预算之外。
 * `api-prod-cluster-a.example.com/v1` 与 `…-b…` 在上限 20 下都截成
 * `api-prod-cluster…/v1`——显示了却分辨不出。URL 的任何位置都可能是差异所在
 * （主机、路径、端口），所以只有对照**本次实际出现的那一组**才能保证可分辨。
 *
 * 做法：从 `max` 起逐步放宽预算，直到全组互不相同；到 `hardMax` 仍撞车就返回原文
 * （宁可撑宽一列，也不能给出分不出来的两行）。组内只有一个 URL 时不存在撞车问题。
 */
export function shortenUrlSet(
  urls: string[],
  max: number,
  hardMax = 64,
): Map<string, string> {
  const uniq = [...new Set(urls)];
  for (let n = max; n <= hardMax; n += 4) {
    const out = new Map(uniq.map((u) => [u, shortenUrl(u, n)]));
    if (new Set(out.values()).size === uniq.length) return out;
  }
  // 兜底返回**原文**（连 scheme 一起）：循环里每一轮都已经做过「去掉 scheme」，这里
  // 再剥一次不增加区分能力，反而会把「两个 URL 只差 http/https」那一类压成同一串——
  // 同一台机器先后换过是否走 TLS 就会这样，那一类只有留着 scheme 才分得开。
  //
  // 但撞到 hardMax 的不止那一类：尾段长到主机头分不到预算时，压短会退化成纯头部省略
  // （见 shortenUrl 的 budget <= 3 分支），于是两条共享长尾段、主机名只在**中间**不同
  // 的地址——api-a.corp.example.com/openai/… 与 api-b.corp.example.com/openai/…——每一
  // 轮都压成同一串，同样落到这里；这一类原文本身就分得开，不靠 scheme。两类都覆盖在
  // 用例里。宁可撑宽一列，也不能给出分不出来的两行。
  return new Map(uniq.map((u) => [u, u]));
}
