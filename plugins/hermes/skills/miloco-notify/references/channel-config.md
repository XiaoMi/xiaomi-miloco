# 通知渠道配置（IM Channel 绑定）

> 这里只管「IM 通知发到哪个对话」这一件事，和通知主流程（选人 / 选渠道 / 写文案）解耦。
> 两种情况会用到本文：①用户主动要绑定渠道；②主流程发 IM 时工具返回 `needsBind`。

## 一、用户主动绑定渠道

当用户说「把通知发到这里」「通知到当前对话」「绑定通知频道」时：

1. 调用 `miloco_notify_bind()`（**无需传参**，自动用当前 sessionKey；工具会校验当前 session 是否有效，无效则拒绝）。
2. 成功后告诉用户配置完成，后续提醒 / 定时任务 / 告警都会发到这个对话。

## 二、发 IM 时遇到 `needsBind`（主流程会撞到这里）

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
| `not_configured` | 尚未设置 Miloco 通知频道，本次临时发到最近对话 | 您尚未设置 Miloco 通知频道，本条消息已临时发送到最近活跃的对话。回复「绑定通知频道」可将当前对话设为固定的 Miloco 通知频道，后续提醒、定时任务、告警等通知都将发送至此。 | You haven't set a Miloco notify channel yet, so this message was sent to your most recent conversation for now. Reply "bind notify channel" to set this conversation as your fixed Miloco notify channel — future reminders, scheduled tasks, and alerts will all be delivered here. |
| `configured_but_invalid` | Miloco 通知频道已失效，本次临时发到最近对话 | 您原先绑定的 Miloco 通知频道已失效，本条消息已临时发送到最近活跃的对话。请回复「绑定通知频道」重新绑定。 | Your previously bound Miloco notify channel is no longer valid, so this message was sent to your most recent conversation for now. Reply "bind notify channel" to re-bind. |
