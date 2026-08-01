# miloco local-vision

Miloco 感知的**本地 GPU 视觉边车**。miloco 后端把一段视频交给它,拿回场景描述与
逐条规则判定 —— 整条通路不需要任何模型厂商 API Key,画面不出本地。

参考实现用微软开源的 [Mage-VL](https://huggingface.co/microsoft/Mage-VL)(Apache-2.0,
4B,codec-native)。但**契约与模型无关**:任何实现同一 HTTP 接口的服务都能替换它,
miloco 侧不需要改一行代码。

## 为什么是独立服务

- **不把 torch/CUDA 拉进 miloco 主包**。miloco 的目标硬件是 Mac mini / 树莓派这类
  CPU-only 机器,主包不该为一个可选功能背上几个 GB 的 GPU 依赖。边车可以跑在另一台
  带显卡的机器上。
- **不接管模型进程的生命周期**。miloco 不下载权重、不拉起也不重启推理进程 ——
  这条边界是有教训的(见上游 #144:1.x 时代由 miloco 管理本地模型容器,故障面扩散到
  显卡直通/驱动/容器,最终无人能支持)。起服务是部署者的事,文档管够。
- **不用 SGLang 的 OpenAI 兼容端点**。那条路把视频按帧当 `image_url` 发
  (见模型自带 `inference.py::run_online`),既拿不到 codec-native 的 token 削减,
  也拿不到流式门控。本服务走进程内 offline API,两者都保住。

## 安装

```bash
cd services/local-vision
python -m venv .venv && . .venv/bin/activate

# torch 必须按自己的 CUDA 版本装,不在依赖里写死。
# 例:RTX 50 系(Blackwell, sm_120)需要 cu128 及以上
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128

pip install -e .
```

另需系统上有 `ffmpeg` / `ffprobe`(codec 通路要用它们探帧与抽运动矢量)。

权重可以预先下好,避免首次启动等下载:

```bash
hf download microsoft/Mage-VL --local-dir ~/data/mage-vl/Mage-VL
```

## 运行

```bash
LOCAL_VISION_CHECKPOINT=~/data/mage-vl/Mage-VL \
LOCAL_VISION_DEVICE=cuda:0 \
LOCAL_VISION_BACKEND=codec \
miloco-local-vision --port 18800
```

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `LOCAL_VISION_CHECKPOINT` | `microsoft/Mage-VL` | 本地权重目录或 HF repo id |
| `LOCAL_VISION_DEVICE` | `cuda:0` | 放哪张卡 |
| `LOCAL_VISION_BACKEND` | `codec` | `codec`(推荐)或 `frames` |
| `LOCAL_VISION_NUM_FRAMES` | `32` | codec 的 target canvas / 帧采样数 |
| `LOCAL_VISION_TOKEN` | 空 | 配了就强制 `Authorization: Bearer`;不配则**必须**只绑环回地址 |
| `LOCAL_VISION_HOST` | `127.0.0.1` | 默认只监听本机 |

然后在 miloco 的「模型」页把感知后端切到「本地 GPU」,填上本服务地址即可。

## 接口

```
GET  /health        → {status, model_loaded, gate_available, gate_error, device, backend}
POST /v1/perceive   → {caption, rule_hits[], gate_p, backend, timing_ms, raw}
```

`/v1/perceive` 请求体:

```json
{
  "video_b64": "<mp4 段的 base64>",
  "scene_ask": "请用中文描述这个家庭监控画面…",
  "rules": [{"name": "沙发有人", "query": "有人在客厅沙发上"}],
  "camera_note": "这台对着门口,忽略窗外行人",
  "max_new_tokens": 256,
  "want_gate": true
}
```

`camera_note` 是该机位的自定义说明。它作为**补充**渲染在输出格式约定**之后** ——
它是用户可写的自由文本,若放在格式说明之前,一句「只用一句话回答」就能让
`规则N:` 那几行消失,而 fail-closed 会把这变成"该相机所有规则静默失效"且无任何报错。

| 环境变量(续) | 默认 | 说明 |
| --- | --- | --- |
| `LOCAL_VISION_MAX_INFLIGHT` | `4` | 同时在飞的推理上限,超限回 503。**不要低于 miloco 允许同时启用的摄像头数**(默认 4)—— 低了会让固定几台相机每窗都抢不到槽位,它们上的规则永久不被评估 |

规则判定 **fail-closed**:模型没给出可解析的判定就一律算「未命中」。漏报只是少一次
agent 提醒,误报却会让 agent 对着不存在的事实做决策。

## 实测(RTX 5090,miloco 真实家庭语料)

同一段家庭监控视频,两种视觉后端:

| | frames(均匀采样) | codec-native | 差异 |
| --- | --- | --- | --- |
| prompt tokens | 7338 | **736** | −90% |
| visual patches | 28672 | **2304** | −92% |
| 单次推理 | 0.89s | **0.28s** | 快 3.2× |

端到端(HTTP 往返 + codec + 3 条规则判定):**约 1.7s**;显存峰值 **12 GB**。
作为对照,同一部署的云端多模态通路单次 omni 调用常在 6~18s。

削减幅度高于官方宣称的 75%,原因合理:家庭监控画面基本静止,codec 的运动矢量与
残差本就稀疏 —— 这类场景恰好是 codec-native 的最佳适用面。

中文与规则判定实测(与云端对同一片段的结论逐条比对):

- 中文描述流畅,能读出画面内嵌的时间戳
- 「有人在客厅沙发上」在客厅(有人躺沙发)命中、在卧室不命中;人在**餐桌**旁时
  该规则正确判否 —— 位置区分是有效的,不是瞎猜
- 带规则提问时,场景描述反而比纯描述提问更准(具体问题改善了视觉接地)

## 已知限制

- **纯视觉**。无音频输入,因此不产语音指令与环境音结论。这是刻意的:让看不见音频的
  模型填这些字段只会得到凭画面脑补的结果。需要音频能力请用云端通路。
- **无身份识别**。本通路只描述场景与判断规则,不识别家庭成员。
- **不执行设备动作**。规则命中一律上报给 agent 决策执行。
- **事件门在 Blackwell(sm_120)上不可用**。StreamMind 门控依赖 `mamba_ssm`,而它需要
  用 CUDA ≥ 12.8 的工具链编译才能产出 sm_120 kernel;用 CUDA 12.0 编出来的会在运行时
  报 `no kernel image is available for execution on the device`。服务会自动熄灯门控并在
  `/health` 的 `gate_error` 里说明原因,caption 与规则判定不受影响。要启用:装 CUDA ≥12.8
  工具链后 `TORCH_CUDA_ARCH_LIST=12.0 pip install --no-build-isolation --force-reinstall mamba-ssm`。
- **门控的域外风险**。参考实现的门控在体育解说数据(SoccerNet)上训练,家庭场景属分布外。
  因此 miloco 侧的 `event_gate_threshold` 默认为 `0`(只把概率记进 `timing._gate_p_*` 供观察,
  不据此丢弃任何窗口)。在自家数据上确认可靠后再调高。
- **段太短会自动降级**。codec 分组要求 ≥8 帧,不足时自动回退到帧采样后端(仍可用,
  只是拿不到 token 削减)。

## 测试

```bash
pytest tests/
```

单测覆盖提示词构建与响应解析,样本全部取自模型在真实家庭片段上的**实际输出**
(它并不总按要求的格式回答,解析器必须容忍几种自发写法)。
