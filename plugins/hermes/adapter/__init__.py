"""Hermes 兼容层入站适配进程包。

把 miloco 后端的 ``{action, payload}`` webhook 契约翻译成 Hermes api_server
的同步 chat 调用，使 miloco 可以在不改后端代码的前提下接入 Hermes agent。
"""
