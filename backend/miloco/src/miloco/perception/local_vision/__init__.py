"""本地视觉感知通路(不走云端 API Key)。

``LocalVisionEngine`` 与云端 ``PerceptionEngine`` 并列实现 ``BasePerceptionEngine``,
由 ``PerceptionEngineProxy`` 按 ``perception.engine_backend`` 配置二选一。
"""

from miloco.perception.local_vision.client import LocalVisionClient, LocalVisionError
from miloco.perception.local_vision.engine import LocalVisionEngine

__all__ = ["LocalVisionClient", "LocalVisionError", "LocalVisionEngine"]
