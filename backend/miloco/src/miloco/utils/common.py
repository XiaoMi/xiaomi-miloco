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
    """把要进日志的值里的换行剥掉。

    用户可控的值里带换行符就能伪造出一整行日志 —— 排障时看到的「上一条」可能是攻击者
    写的（CodeQL py/log-injection）。凡是把请求里来的值记进日志的地方都走这里，别在
    各自的 router 里各写一份：漏装的那个入口静默绕过——它自己的日志看着一切正常。

    调用者标识今天恒为 ``None``（鉴权依赖成功时不返回值），但它的类型标注是字符串，
    语义上本就该是调用者身份。剥换行是为了「将来补成返回真实标识」那一天不必回头
    逐处补——**那时补就晚了**。

    两个字符都换成**空格**而不是删掉：删掉会把「关灯\n晚安」拼成「关灯晚安」，与一个
    真就叫那个名字的任务在日志里再也分不开，按名字 grep 会互相串；多行的异常消息同样
    会被拼成一串没有分隔的文字。而「不让它伪造出额外整行」这个目的，换成空格一样达成。
    孤立的回车此前走的正是被反对的删掉那条路，而它恰是回车最常见的出现形态——终端
    回车、老式 Mac 换行、只带回车的异常消息。CRLF 则先整体换一次，免得拆成两次换
    而平白多出一个空格。
    保持 `replace` 首参是**字面量**的形态：CodeQL 只认第一个实参是字面量 "\n" / "\r\n"
    的 replace 为 sanitizer，改成正则或变量会重新触发 py/log-injection 告警。
    """
    if value is None:
        return "None"
    return str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
