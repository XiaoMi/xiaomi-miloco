# ONNX Runtime 依赖

## L1：它是什么

ONNX Runtime 是微软开源的跨平台推理引擎，支持运行 ONNX 格式的机器学习模型。Miloco 用它在本地运行感知流水线中的多类模型（视觉检测 / ReID、语音 VAD、文本句向量嵌入），无需外部 API 调用，保护隐私且延迟可控。

---

## L2：我们怎么用

### 活跃模型

| 模型文件                      | 用途                                                                                       | 必需性 |
| ----------------------------- | ------------------------------------------------------------------------------------------ | ------ |
| `det_4C.onnx`                 | 目标检测：输出人体 / 人头 / 人脸 / 宠物（猫狗）等目标的检测框与置信度，是感知运行的前提    | 必需   |
| `human_body_reid_v2.onnx`     | 人体 ReID：提取外观嵌入用于跨帧身份关联，也用于陌生人池跨 track 去重与 tier_a 注册嵌入提取 | 必需   |
| `silero_vad.onnx`             | 语音活动检测（VAD）：Gate 层判定音频窗口是否含人声，过滤无语音窗口                         | 可选   |
| `bge-small-zh-v1.5-int8.onnx` | 文本句向量嵌入：有价值事件 / 建议的语义去重（需配套 `bge-small-zh-v1.5-tokenizer.json`）   | 可选   |

必需模型缺失则引擎进入 `PREREQ_MISSING` 降级；可选模型缺失只让对应子能力（语音门控 / 语义去重）退化，引擎主链路仍可运行（校验清单见 `perception/engine/resource_validator.py`）。

感知的重推理（检测 / ReID）在专用 `ThreadPoolExecutor`（`perception-infer` 线程）中执行，与主事件循环解耦；VAD / bge 这类可选轻量模型各自内联创建 CPU session。运行时模型从 `directories.models_dir`（默认 `$MILOCO_HOME/models/`）加载，包内 `perception/models/` 作兜底。

### Session 创建与平台适配

检测 / ReID 模型的 ONNX Runtime session 统一由 `make_session`（`perception/inference/ort_utils.py`）创建，集中控制线程数并按平台选择 Execution Provider：默认 CPU EP，`use_gpu` 且可用时优先 CUDA EP，Apple Silicon 上则优先 CoreML EP。

ONNX Runtime 的 ARM CPU EP 默认走的 ArmKleidiAI 卷积路径曾存在 native workspace 内存不归还问题，长跑 RSS 单调上涨。当前稳定形态是从依赖层根治：将 onnxruntime 依赖下限抬到含上游修复的版本，覆盖所有走 CPU EP / KleidiAI 卷积的平台（含 Apple Silicon 上 CoreML 不支持算子的 CPU fallback、Linux ARM 等）。`make_session` 内的代码层防御——Apple Silicon 优先 CoreML EP 绕开该路径、并在支持的版本上追加关闭 KleidiAI 的 session 开关——保留作冗余兜底；CoreML 相对 CPU EP 的数值漂移在检测 / ReID 模型的业务阈值内可忽略，故未钉死计算单元。此为 ONNX Runtime SDK 侧的已知限制，属其责任边界。

CoreML EP 另有一处上游已知限制：每建一个 InferenceSession 都会把 ONNX 子图序列化成 ~模型等大的中间 `.mlmodel` 文件写进系统 `$TMPDIR`，而删除只挂在底层 C++ 对象的析构链上——进程被强杀 / session 对象不及时释放即永久遗留（上游 `microsoft/onnxruntime#26023`，至今无修复）；感知侧高频重建 session 会把它放大到撑爆磁盘。这是 ORT / CoreML 侧的责任边界，代码层做有界兜底：给 CoreML session 挂按模型内容 hash 隔离的持久复用缓存目录（`ModelCacheDirectory`，需支持该 option 的较新 runtime，更低版本自动退回不带缓存的 plain CoreML），把中间文件从系统临时目录挪到自家缓存并跨 session / 进程复用——「无界泄漏」收敛为「每模型一份的有界 footprint」；内容 hash 规避上游缓存键不检测模型变更、原地换模型复用旧编译产物的坑。另加进程内一次的总量兜底清理（缓存超阈值则整目录清空重建，仅清自家独占缓存）。缓存逻辑全程 fail-safe，任何异常一律优雅退回无缓存的 plain CoreML，绝不因它拖垮感知推理。相关封装在 `perception/inference/ort_utils.py`。

### 安装时下载校验机制

模型文件存放在 `$MILOCO_HOME/models/` 目录下（路径由 `directories.models` 配置，见 `settings.yaml::directories`）。安装时由 `install.py`（`scripts/install.py`）负责将模型下载 / 恢复到位并校验完整性，具体下载与校验流程见脚本。

模型文件不随 Python wheel 分发（避免包体过大），安装脚本负责确保模型就位。

### 模型缺失时的降级行为

必需模型缺失时引擎不崩溃、而是进入 `PREREQ_MISSING` 降级——启动前校验由 `PerceptionEngineProxy`（`perception/client.py`）在初始化时触发（校验清单见上文 `resource_validator.py`）。降级的契约语义（`503` 拒绝范围、其他功能不受影响）、状态查询方式与补全模型的修复步骤分别见 [感知流水线](../03-features/perception-pipeline.md) 与 [故障排查](../06-dev-guide/troubleshooting.md)，此处不复述。

### 出问题找谁

ONNX Runtime 本身是公开开源库（Microsoft），遇到引擎层问题查 ONNX Runtime 官方 issue。模型文件本身（`det_4C.onnx` / `human_body_reid_v2.onnx`）由小米内部提供，模型质量/精度问题反馈给小米 AI 视觉团队。
