import json
from typing import Any


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深度合并：override 值优先；嵌套 dict 递归合并。"""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def escape_for_js_string(value: str) -> str:
    """把任意字符串编码成可安全嵌进 ``<script>"..."</script>`` 内的 JS 字符串字面量内容。

    - 走 ``json.dumps`` 处理双引号 / 反斜杠 / 控制字符 / 非 BMP 字符（产出始终
      是 ASCII-safe），sliced 掉外层 ``"`` 留下内层字面量。
    - 额外把 ``</`` 替换为 ``<\\/``：``json.dumps`` 不 escape ``<``，原样嵌入
      ``<script>...</script>`` 内可能被运维手改的 ``</script>`` 子串提前闭合
      触发 XSS。``\\/`` 在 JS 字符串里跟 ``/`` 等价但不会被 HTML parser 当作
      标签结束。spa_handler / watch_page 共用同款防御，**改一边记得同步**。
    """
    return json.dumps(value)[1:-1].replace("</", "<\\/")


def safe_log(value: Any) -> str:
    """去掉 CR / LF 再进日志。

    用户可控的值里带换行符就能伪造出一整行日志 —— 排障时看到的「上一条」可能是攻击者
    写的（CodeQL py/log-injection）。凡是把请求里来的值记进日志的地方都走这里，别在
    各自的 router 里各写一份：漏装的那个入口静默绕过。
    """
    if value is None:
        return "None"
    return str(value).replace("\r", "").replace("\n", " ")
