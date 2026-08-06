"""ONNX Runtime 线程上限——只放调参常量,不含实现(与 miot/tuning.py 同款)。

放这里而不是 ort_utils.py:后者是 module 级 `import onnxruntime`,消费方为拿一个
int 就得把 ORT 原生库拉进自己的 import 图。speech_vad 在 gate 的 eager 链上,且
VAD 关掉时(evaluate_speech 早退)根本不建 session,不该为此加载原生库。
"""

# dedup(句向量,~10ms/短句、低频)/ vad(silero,单帧 <1ms、逐帧高频):模型极小,
# 单线程最优,多线程 fork-join 开销 > 收益。
TINY_MODEL_THREADS = 1
