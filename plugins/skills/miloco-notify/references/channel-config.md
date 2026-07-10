# 通知渠道配置（IM Channel 绑定）

> 这里只管「IM 通知发到哪个对话」这一件事，和通知主流程（选人 / 选渠道 / 写文案）解耦。
> 两种情况会用到本文：①用户主动要绑定渠道；②主流程发 IM 时工具返回 `needsBind`。

## 一、用户主动绑定渠道

当用户说「把通知发到这里」「通知到当前对话」「绑定通知频道」时：

1. 调用 `miloco_notify_bind()`（**无需传参**，自动用当前 sessionKey；工具会校验当前 session 是否有效，无效则拒绝）。
2. 这是**追加绑定**：当前对话会加入 Miloco 的 IM 通知通道列表；重复绑定同一对话是幂等的，不会重复添加。
3. 成功后告诉用户配置完成，后续提醒 / 定时任务 / 告警都会发到**所有已绑定对话**。

## 二、用户主动解绑渠道

当用户说「不要再把通知发到这里」「解绑通知频道」「取消当前对话接收通知」时：

1. 调用 `miloco_notify_unbind()`（**无需传参**，自动解绑当前 sessionKey）。
2. 若用户明确要求「清空全部通知通道 / 解绑所有通知频道」，调用 `miloco_notify_unbind(all=true)`。
3. 成功后告诉用户解绑结果；若当前对话本来就没绑定，也按成功处理即可。

## 三、发 IM 时遇到 `needsBind`（主流程会撞到这里）

`miloco_im_push` 在尚未绑定（或绑定已失效）时**不会直接发出**，而是返回：

```json
{ "ok": false, "needsBind": true,
  "bindReason": "not_configured | configured_but_invalid",
  "bindHintExample": "<可直接翻译的引导语范例>",
  "nextAction": "..." }
```

收到 `needsBind:true` 时，**这是让你继续操作的信号，不是回复给用户的内容**——本条通知此刻还没发出。照下面三步走：

1. **不要在对话里回复用户、也不要等待确认。**
2. 把返回里的 `bindHintExample` **翻译成用户当前使用的语言**（范例是中文，按需改写；与下方模板一致）。
3. 带上**原样不变的 `message`** + 翻译好的 `bindHint` **再次调用** `miloco_im_push`——工具会把 `bindHint` 拼到正文之后投递到最近活跃的对话。**不要**手动把引导语塞进 `message`。

补上 `bindHint` 重发后，通知才真正送达。

### bindHint 模板（按用户语言改写，下面只给 zh / en 示例）

| bindReason | 含义 | 中文示例 | English |
| ---- | ---- | ---- | ---- |
| `not_configured` | 尚未设置 Miloco 通知频道，本次临时发到最近对话 | 您尚未设置 Miloco 通知频道，本条消息已临时发送到最近活跃的对话。回复「绑定通知频道」可将当前对话加入固定的 Miloco 通知频道列表，后续提醒、定时任务、告警等通知都会发送到所有已绑定通道。 | You haven't set a Miloco notify channel yet, so this message was sent to your most recent conversation for now. Reply "bind notify channel" to add this conversation to your fixed Miloco notify channel list — future reminders, scheduled tasks, and alerts will be delivered to all bound channels. |
| `configured_but_invalid` | 原先绑定的 Miloco 通知频道已全部失效，本次临时发到最近对话 | 您原先绑定的 Miloco 通知频道已全部失效，本条消息已临时发送到最近活跃的对话。请回复「绑定通知频道」重新加入有效通道。 | Your previously bound Miloco notify channels are no longer valid, so this message was sent to your most recent conversation for now. Reply "bind notify channel" to add a valid channel again. |
