"""HermesAdapter —— Plugin 侧,backend 通过 ``miloco.agent_platform.loader`` 动态加载。

本目录由 ``install-hermes.sh`` Step 4.x 复制到 ``$MILOCO_HOME/agent_platform/hermes/``,
backend 启动时按 ``settings.agent.platform == "hermes"`` 加载。

设计原则(对齐 ``hermes-pr.md`` §五 #1+#2+#11):
- 不依赖 backend wheel(duck-typed,不 import ``miloco.agent_platform.base``)
- ``build_system(profile, extra)``: 静态 + 动态块对齐 ``context_injection.resolve_profile``
  分级,组装 OpenAI ``<system>`` 消息文本
- ``send_turn(ctx)``: 同步 POST 到 Hermes ``/v1/chat/completions``,携带 system msg + user msg,
  带 ``X-Hermes-Session-Id`` 头做会话连续;溢出自愈沿用 PR #279 的 best-effort(无 session 重试)
- ``read_trace_meta(run_id)``: 读 ``$MILOCO_HOME/trace/<run_id>.meta.json``(plugin trace.py 写);
  文件不存在 / 解析失败 → 返回 None,backend poller 走超时分支
"""