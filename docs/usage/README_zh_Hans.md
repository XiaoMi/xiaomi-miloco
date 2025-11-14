# 使用教程

<div align="center">

简体中文 | [English](README.md)

</div>

智能家居未来探索方案 **Xiaomi Local Copilot** ，以米家摄像机为视觉信息来源，以自研大模型为核心，打通全屋 IoT 设备。基于大模型的开发范式，让用户能够以自然语言定义家庭的各种需求和规则，实现更广泛、更具创意的智能联动。
你可以用跟它对话，实现：
- **随时提问**。如“[看看画面里有什么](https://www.bilibili.com/video/BV1fACsBzEgn/?share_source=copy_web&vd_source=a325cb1aeca5ec6a025e5978183ab07c)“看看我的猫在哪”

- **创建规则**。如“[当有人在读书时，打开台灯](https://www.bilibili.com/video/BV1BACsBzELs/?share_source=copy_web&vd_source=a325cb1aeca5ec6a025e5978183ab07c)”“当有人在玩手机时，发送米家通知提醒”

- **复杂控制**。如“看看小孩有没有写作业？没有写作业的话音箱播放该写作业了”“[根据画面中羽绒服的颜色来设置灯带的颜色](https://www.bilibili.com/video/BV1BACsBzEVp/?share_source=copy_web&vd_source=a325cb1aeca5ec6a025e5978183ab07c)”

- **动态推理**。如“[当画面里面有人时，根据画面中人的穿衣风格来生成赞美文案，然后用智能音箱播报生成的赞美文案](https://www.bilibili.com/video/BV1ZACsBzE8y/?share_source=copy_web&vd_source=a325cb1aeca5ec6a025e5978183ab07c
)”“当画面里有人在睡觉，根据画面中人是否盖被子来调整空调的温度”

## 基础配置

开始使用前，请跟随以下教程完成基础设置

**登录码设置**
- 设置6位数登录码，支持数字与英文字母组合。请务必牢记，下次在新环境登录时需要使用。

**授权**
- 小米账号授权：首次登录时通过小米账号进行登录授权，可实现对米家设备、米家场景列表的获取与执行。
- HA 授权：在设置-授权设置页面，通过 Token +API 配置授权，可实现对 Home Assistant 场景的获取与执行

  <img src="../../assets/images/HA_Authorization_cn.PNG" width="45%" />


**模型设置**
- 在模型管理页面可进行模型配置与管理：
  - 规划大模型用作 Query 意图的分析、任务规划与工具调用，推荐使用云端模型。

  - 视觉理解大模型用作摄像头视觉信息处理与理解，推荐使用本地 Xiaomi MiMo-VL-Miloco 7B模型。

    <img src="../../assets/images/model_management_cn.jpg" width="60%" />


## 开始使用

### 如何创建规则

#### 规则卡片介绍

**规则名称**：必填。为当前规则选择一个合适的名称。

**选择生效的摄像头**：必填，支持单选或多选。选择的摄像头将成为当前规则触发条件分析的视觉来源。

**触发条件**：必填。可用自然语言描述触发条件，推荐句式“当...的时候”，触发条件需要表述清晰、准确。

**执行动作**：必填，条件触发后的执行动作，支持单个或多个动作同时触发

1. MCP：选择规则触发需要使用到的 MCP 服务，支持单选与多选

2. 设备控制：可输入自定义执行动作。如选择此项，必须点击测试生成指令后方可保存。可自行选择是否缓存指令。

    - 缓存指令后，规则触发时将执行测试时缓存的指令。举例：“打开台灯”。测试时打开了台灯A，后续每次触发皆打开台灯A。流程快，耗时短。
    - 不缓存指令，规则触发时将进行实时推理并执行。举例：“根据画面人的衣服颜色调整灯光的颜色”，后续执行效果将根据画面中实时推理的颜色来执行。耗时较长，根据需求选择。

3. 自动化场景：可选择执行米家与 HA 场景，支持多选与单选。

4. 发送米家通知：支持自定义米家通知内容并发送

    <img src="../../assets/images/Rule_Card_cn.PNG" width="40%" />


**高级配置：非必填项**，支持选择规则触发的时间段与触发间隔
1. 触发时间段：支持全天、白天、晚上三个时间段
2. 最小触发间隔：设置规则的最小触发间隔时间。

    <img src="../../assets/images/Minimum_Trigger_Interval_cn.png" width="60%" />

#### 方法1：手动创建规则
在规则管理页面，点击添加规则，按需完成规则内容填写即可。[参考视频](https://www.bilibili.com/video/BV1ZACsBzE8y/?share_source=copy_web&vd_source=a325cb1aeca5ec6a025e5978183ab07c)

#### 方法2 ：自动创建规则

实现方式：在 AI 中心-对话框内用自然语言描述规则类需求，大模型调用创建规则 Agent 自动创建规则。

1. 选择触发规则生效的摄像头范围，如未选择则默认大模型自动选择；选择触发规则需要使用的 MCP 服务。完成配置后，用自然语言描述规则类需求，点击发送。

2. 大模型分析用户发送的 Query，调用创建规则 Agent，自动填充规则卡片，用户可以选择修改大模型生成的规则卡片内容。检查卡片内容无误后，可点击保存规则，即创建成功。

3. [参考视频](https://www.bilibili.com/video/BV1BACsBzELs/?share_source=copy_web&vd_source=a325cb1aeca5ec6a025e5978183ab07c)

### 如何配置模型

#### 添加云端模型

1. 模型管理页面，点击添加模型功能按钮，出现云端模型配置弹窗。

2. 输入Base URI+API Key后，会自动拉取模型列表；部分云端模型不提供自动拉取列表的接口，需要手动输入模型名称。

3. 点击确定后即可保存。

#### 添加本地模型： 

启动脚本中会自动下载并配置有默认模型，也可自行通过以下方式自定义模型：
1. 修改 `config/ai_engine_config.yaml` 文件中的 models 配置：添加/修改自定义模型名称、配置项等，将 model_path（mmproj_path）指向本地模型文件的绝对地址。
2. 重新启动推理引擎服务

#### 高级设置

规划大模型用作 Query 意图的分析、任务规划与工具调用，使用云端模型能获得更好的效果。

视觉理解大模型用作摄像头视觉信息处理与理解，推荐使用小米自研端侧模型。

### 如何配置 MCP 服务
1. 完成授权后，可使用米家自动化 ( MIoT Automation )、米家设备控制 ( MIoT Device Control )、HA 自动化三个已封装的 MCP 能力。其他第三方 MCP 服务需用户自行开发配置。
2. 在 MCP 服务页面可通过“添加服务”功能添加 SSE、HTTP、Stdio 三种类型的 MCP 服务。配置完成后，可在AI 中心对话中选择 MCP 服务使用。
 示例：选择 MCP 服务类型，输入名称、URL、请求头后成功接入一个天气 MCP。 在对话时选择天气 MCP，可进行天气查询。

    <img src="../../assets/images/MCP_Services_cn.png" width="40%" />  <img src="../../assets/images/Use_MCP_Services_cn.png" width="40%" />
