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

## 环境要求

| | 要求 | 为什么 |
| --- | --- | --- |
| 操作系统 | Linux x86_64 / aarch64 | `codec-video-prep` 只发 Linux wheel(无 sdist);macOS 装不上 |
| Python | 3.10 – 3.12 | 同上,且它要求 `numpy<2.0`,而符合该约束的最新 numpy 也只到 cp312 |
| GPU | NVIDIA,显存 ≥ 16 GB | 实测 4B 模型峰值 12 GB |
| 系统工具 | `ffmpeg` / `ffprobe` | codec 通路用它们探帧与抽运动矢量 |

在 3.13/3.14 上 `pip install -e .` 会直接失败(或退化成源码编译 numpy,需要一整套
编译工具链)—— 请用 3.12 及以下建 venv。

## 安装

```bash
# 从仓库根目录开始
cd services/local-vision
python3.12 -m venv .venv && . .venv/bin/activate

# torch 必须按自己的 CUDA 版本装,不在依赖里写死。
# 例:RTX 50 系(Blackwell, sm_120)需要 cu128 及以上
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128

pip install -e .

# 想跑测试再加 dev extra(pytest 不在运行依赖里)
pip install -e '.[dev]'
```

**装的顺序有讲究**:`accelerate` 依赖 `torch`,所以**必须先**按上面那行装好对应 CUDA
版本的 torch,再 `pip install -e .`。反过来的话,pip 会静默装上 PyPI 的通用 torch,
在 RTX 50 系上跑起来是错的,而症状要到推理时才以「no kernel image」之类的形式出现。

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

命令行参数 `--checkpoint` / `--device` / `--backend` / `--port` 会覆盖对应环境变量。

**跑在另一台机器上**时必须配访问凭证 —— 未配 token 时服务会**拒绝**绑定非环回地址
(那等于把家里画面的推理接口无鉴权地放到局域网上):

```bash
LOCAL_VISION_TOKEN=$(openssl rand -hex 16) \
LOCAL_VISION_HOST=0.0.0.0 \
LOCAL_VISION_CHECKPOINT=~/data/mage-vl/Mage-VL \
miloco-local-vision --port 18800
```

然后在 miloco 的「模型」页把同一个 token 填进「访问凭证」。

> 本服务用 `trust_remote_code=True` 加载模型,也就是会执行随权重一起下载的代码。
> 这是 Mage-VL 这类自定义架构的常规要求,但把它指向自家摄像头之前值得知道这件事。

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `LOCAL_VISION_CHECKPOINT` | `microsoft/Mage-VL` | 本地权重目录或 HF repo id |
| `LOCAL_VISION_DEVICE` | `cuda:0` | 放哪张卡 |
| `LOCAL_VISION_BACKEND` | `codec` | `codec`(推荐)或 `frames` |
| `LOCAL_VISION_NUM_FRAMES` | `32` | codec 的 target canvas / 帧采样数 |
| `LOCAL_VISION_TOKEN` | 空 | 配了就强制 `Authorization: Bearer`;不配则**必须**只绑环回地址 |
| `LOCAL_VISION_MAX_PIXELS` | `150000` | 单帧视觉预算(像素)。两条通路共用;下方实测数字就是按这个默认值测的 |
| `LOCAL_VISION_ATTN` | `sdpa` | transformers 的 attention 实现 |
| `LOCAL_VISION_HOST` | `127.0.0.1` | 默认只监听本机 |
| `LOCAL_VISION_PORT` | `18800` | 等价于 `--port` |
| `CV_PREINFER_BIN` | 自动定位 | codec 通路依赖的外部二进制;不在 PATH 上时用它显式指定 |
| `LOCAL_VISION_MAX_INFLIGHT` | `5` | 同时在飞的推理上限,超限回 503。**必须 > miloco 允许同时启用的摄像头数**(默认 4):等于相机数时,一次主动查询只要撞上正在进行的实时窗口就会全部 503;低于相机数时,固定几台相机每窗都抢不到槽位、规则永久不被评估。注意默认值 5 仍不足以让"实时窗口 + 主动查询"两批各 4 台完全并行(4+4>5),此时主动查询会有相机拿不到答案(静默略过该相机);相机多且常用主动查询的部署请调高 |

然后在 miloco 的「模型」页把感知后端切到「本地 GPU」,填上本服务地址即可。

`/health` 不鉴权,但会回 `auth_required` / `auth_ok`——miloco 靠它在**切换那一刻**
就发现 token 配错,而不是等到每一窗推理 401、界面却一路绿灯。代价是它也回答了
"这个 Bearer 值对不对";比较是常数时间的,且 `/v1/perceive` 本来就是同样的判据
(鉴权在请求体校验之前),所以不是新增能力——但反向代理白名单里通常只放 `/health`,
把它暴露到不受信网络前请留意这一点。

## 接口

```
GET  /health        → {status, model_loaded, gate_available, gate_error, device, backend,
                       auth_required, auth_ok}
POST /v1/perceive   → {caption, rule_hits[], unparsed_rules, truncated, gate_p, backend,
                       timing_ms, raw}
```

自行实现本契约时,下面几个字段**必须**返回,否则 miloco 侧的安全判定会退化:

| 字段 | 不返回的后果 |
| --- | --- |
| `auth_required` / `auth_ok` | 凭证校验**fail-open**:miloco 读不到就当作"不需要鉴权",于是配错 token 的部署探活全绿、每一窗推理 401,感知静默停摆 |
| `unparsed_rules` | 「模型确实说不」与「输出被复读/截断吃掉」变得无法区分,后者会把该相机的规则整体推成未命中 |
| `truncated` | 同上,且描述会从句子中间断掉后原样交给 agent |
| `rule_hits[].name` | 调用方先按 `name` 认领判定,认不出来就**丢弃**这一条(不按下标猜——猜错会让「厨房明火」背上「有人跌倒」的结论)。请逐条返回、顺序与请求里的 `rules` 一致,`name` 原样回填 |
| `rule_hits[].hit` / `.reason` | `hit` 是判定本身;`reason` 是依据,会进事件文本给 agent 看 |

请求侧的上限:`rules` 最多 64 条,`scene_ask` ≤ 4000 字符,`camera_note` / 规则
`query` ≤ 2000 字符,视频段解码后 ≤ 64 MiB。超限一律 422(视频超限为 413)。

状态码:`401` 凭证缺失或不匹配;`413` 视频段超过 64 MiB;`422` 请求体不合法
(base64 坏了、空载荷、字段越界);`503` 模型未就绪(`engine not ready`)或在飞请求
已达上限(`busy: …`,调用方应把这一窗当作无结论跳过);`500` 推理本身失败。

`/v1/perceive` 请求体:

```json
{
  "video_b64": "<mp4 段的 base64>",
  "scene_ask": "请用中文描述这个家庭监控画面…",
  "rules": [{"name": "沙发有人", "query": "有人在客厅沙发上"}],
  "camera_note": "这台对着门口,忽略窗外行人",
  "max_new_tokens": 256,
  "want_gate": true,
  "ngram_guard": 32
}
```

`/health` 里的 `backend` 是**启动时配置**的值。实际用哪条通路是每次请求现算的
(段太短或缺 ffprobe 会退到 `frames`),真实值在 `/v1/perceive` 响应的 `backend`
字段里 —— miloco 把它记进感知耗时的 `_video_backend`。

`camera_note` 是该机位的自定义说明。它作为**补充**渲染在输出格式约定**之后** ——
它是用户可写的自由文本,若放在格式说明之前,一句「只用一句话回答」就能让
`规则N:` 那几行消失,而 fail-closed 会把这变成"该相机所有规则静默失效"且无任何报错。


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

## 排查

| 症状 | 多半是 |
| --- | --- |
| `/health` 一直 `"status":"loading"` | 看 `load_error` 字段:非空说明加载**失败**(权重路径打错最常见),不会自己好;为空才是真的在加载 |
| 每次响应都是 `"backend":"frames"` | 缺 `ffprobe`,或段太短(< 8 帧)。前者会让 token 削减完全失效,值得先查 |
| `gate_available` 起初为 true、之后变 false | 门控要到第一次推理才真正跑起来;失败原因见 `gate_error` |
| miloco 侧显示「边车拒绝当前凭证」 | 两边的 `LOCAL_VISION_TOKEN` 与「访问凭证」不一致 |

## 已知限制

- **没有画面变化门控**。云端通路有一层帧差/音量门,静止画面直接跳过不调模型;
  本通路对每台相机、每个窗口都会调用一次边车(`perception.engine.gate.*` 对它无效)。
  这是刻意的——本地推理不计费,漏掉事件的代价高于多跑一次的代价。但它意味着 GPU
  是常态占用的,规划功耗时按这个算。
- **纯视觉**。无音频输入,因此不产语音指令与环境音结论。这是刻意的:让看不见音频的
  模型填这些字段只会得到凭画面脑补的结果。需要音频能力请用云端通路。
- **单台相机的规则条数有上限**。生成预算按条数放大但封顶 1024 token,约 32 条之后
  每条能分到的篇幅开始变窄,末尾的判定可能被截断 —— 而 fail-closed 会把截断变成
  静默的未命中(`truncated` / `unparsed_rules` 会亮,日志里有 WARNING)。请求侧硬
  上限 64 条。注意**未指定相机的规则会广播到每一台**,所以这个数是按相机算的。
- **画面越复杂越慢,而且相机之间是串行的**。单卡单模型,边车用一把锁串行推理,
  所以一个窗口的耗时约等于各相机推理耗时之和。实测真实家庭画面单台 1.4–2.0s ——
  也就是说 2 台就接近 4s 的默认周期,再多就会追不上(miloco 会在日志里提示一次)。
  相机多的部署请调大 `perception.engine.input.period_sec`,或把 `max_new_tokens` /
  `video_short_edge` 调小。
- **不产主动建议**。云端通路会从画面里生成 suggestion 推给 agent;本通路只产描述与
  规则判定,`suggestions` 恒为空。
- **无身份识别**。本通路只描述场景与判断规则,不识别家庭成员。
- **不执行设备动作**。命中只作为观察结论上报给 agent(不含规则里配置的动作)。
  带直连设备动作的规则会被拒绝切换到本通路;若已在本通路,这类动作会被拒绝执行
  并在规则执行历史里记为一次失败触发。
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
pip install -e '.[dev]'   # pytest 不在运行依赖里
pytest tests/             # 在 services/local-vision 目录下
```

单测覆盖提示词构建与响应解析,样本全部取自模型在真实家庭片段上的**实际输出**
(它并不总按要求的格式回答,解析器必须容忍几种自发写法)。
