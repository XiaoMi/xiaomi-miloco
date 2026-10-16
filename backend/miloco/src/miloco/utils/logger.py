# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Global logger configuration.
Provides warning capture and deprecation warning filters via the logging system.
"""

import logging
import warnings

from miloco.utils.common import safe_log

# 过滤掉来自第三方依赖的警告
SUPPRESSED_DEPRECATION_PATTERNS: list[str] = [
    "websockets.legacy is deprecated",
    "websockets.server.WebSocketServerProtocol is deprecated",
    "'asyncio.iscoroutinefunction' is deprecated",
]


class DeprecationWarningFilter(logging.Filter):
    """Filter that suppresses known third-party DeprecationWarning messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(pattern in msg for pattern in SUPPRESSED_DEPRECATION_PATTERNS)


def setup_warning_filters() -> None:
    """Capture warnings into the logging system and attach deprecation filters.

    Call this once during application startup, before uvicorn.run().
    """
    # Route all warnings through the logging system (logger: "py.warnings")
    logging.captureWarnings(True)

    warnings_logger = logging.getLogger("py.warnings")
    warnings_logger.addFilter(DeprecationWarningFilter())

    # Also suppress at the warnings module level for messages emitted at import
    # time (before logging capture is active).
    for pattern in SUPPRESSED_DEPRECATION_PATTERNS:
        warnings.filterwarnings(
            "ignore", message=f".*{pattern}.*", category=DeprecationWarning
        )


class ColoredFormatter(logging.Formatter):
    """给【感知流程报错】的 WARNING / ERROR 上色(灰底黄/红),便于 ``tail -f`` 时
    一眼定位真错误。

    - **只染**消息含 ``_COLOR_MARKERS`` 模块标签的条目——即 MR214(fix/perception-error-log-wording)
      统一成 ``[模块] 描述 | %s`` 的感知报错(``[engine]`` / ``[omni]`` / ``[pipeline]`` /
      ``[processor]`` / ``[collect]`` / ``[runner]``);其余无标签的常态噪音(``收到 dup_id 标记`` /
      ``stream_buffer overflow`` 等)**不染**,避免把日志刷成一片黄/红。
    - 染色时仅裹 levelname 与 message;asctime、logger name 保持原色。INFO/DEBUG 不染。
    - 要扩大/缩小染色范围,改 ``_COLOR_MARKERS`` 即可。

    ⚠️ 色码(ANSI)会写进日志文件本身:``tail`` / ``less -R`` 正常渲染彩色,
    ``grep`` 按文本仍可匹配(色码只裹在 token 两侧、不插在字内),但严格解析或
    不支持 ANSI 的工具会看到 ``\\x1b[..m`` 转义。住户排障日志可接受;若不想要
    可把 uvicorn 的 formatter 切回纯 ``logging.Formatter``。
    """

    _RESET = "\033[0m"
    # 48;5;240 = 256 色里的中灰底;93 = 亮黄字,91 = 亮红字。
    _COLORS: dict[int, str] = {
        logging.WARNING: "\033[48;5;240;93m",
        logging.ERROR: "\033[48;5;240;91m",
        logging.CRITICAL: "\033[48;5;240;91m",
    }
    # 仅含这些模块标签的 WARNING/ERROR 才染色——即 fix/perception-error-log-wording(MR214)
    # 统一成 ``[模块] 描述 | %s`` 的感知流程报错。其余无标签的常态噪音(dup_id/overflow 等)不染。
    _COLOR_MARKERS: tuple[str, ...] = (
        "[engine]",
        "[omni]",
        "[pipeline]",
        "[processor]",
        "[collect]",
        "[runner]",
    )

    def format(self, record: logging.LogRecord) -> str:
        color = self._COLORS.get(record.levelno)
        if color is None:
            return super().format(record)
        rendered = record.getMessage()  # 先按原 args 渲染消息
        # 只给白名单 marker 的消息染色;其余 WARNING/ERROR(dup_id / overflow 等噪音)原样输出。
        if not any(m in rendered for m in self._COLOR_MARKERS):
            return super().format(record)
        # 临时把 levelname / message 裹上色码再交给父类格式化;格式化后立刻还原,
        # 防同一 LogRecord 被其它 handler / formatter 复用时带上色码或重复渲染。
        orig_levelname = record.levelname
        orig_msg = record.msg
        orig_args = record.args
        record.levelname = f"{color}{orig_levelname}{self._RESET}"
        record.msg = f"{color}{rendered}{self._RESET}"
        record.args = None  # 已渲染,清空避免父类二次 % 格式化
        try:
            return super().format(record)
        finally:
            record.levelname = orig_levelname
            record.msg = orig_msg
            record.args = orig_args


def cam_tag(camera_id: str, channel: int) -> str:
    """把「相机 + 通道」拼成一个可入日志的标识。

    这两个值在日志里总是成对出现、指的是同一路码流，拼一次比每处都写
    ``%s.%d`` 整齐，也让格式串少一个占位符——而「数值占位符配上字符串实参」这种
    会让整行日志在运行期被吞掉的错配，在结构上也就不可能发生了。顺带剥掉换行：
    相机标识是路径参数、外部可控，而拼接之后就分不出哪一段来自请求，所以在这里剥
    而不在各个日志点包。

    剥换行那份实现在 :func:`miloco.utils.common.safe_log`，本函数只是它的一个
    拼装调用方；接口层与服务层都要打这一对值，两边都从那里取。

    落点必须是「导入了清洗函数」的模块，别因为「跟 safe_log 就近」把它搬进
    :mod:`miloco.utils.common`：本函数按裸名字调用 ``safe_log``，而
    ``test_oauth_error_shape`` 里那条「已在调用清洗函数的文件不许掉出扫描范围」
    的护栏，拿「文件里确实调用了清洗函数」当地面真值、范围则由「从清洗落点导入过
    它的文件」∪「定义了路由的文件」两半构成 —— 定义落点的那个模块自己既不导入、
    也不定义路由，写在那里就成了一处护栏看不见的调用方。改落点前先读那条护栏。
    """
    return safe_log(f"{camera_id}.{channel}")
