---
name: miloco-miot-scope
description: 感知范围控制 — 管理 miloco 感知哪些家庭、哪些摄像头，每台摄像头的声音（是否参与感知），每台摄像头的「感知须知」（专属 prompt，补机位环境 / 关注 / 忽略以提升感知准确性），以及每台摄像头的「智能裁切增强」（Smart Crop，送模型的画面构图：裁主体 vs 送整幅）。用户说「只用/不用某家庭」「让 miloco 感知/别感知 某家庭或摄像头」「屏蔽某摄像头/把某摄像头从感知里去掉」「把某摄像头声音关了/打开」「XX 老误报（声音类）」「哪些家庭在用」时激活；也在用户吐槽**画面/视觉类固定误识**时激活——「门口摄像头老把电梯门当成我家门」「XX 老把 Y 认成 Z」「这台摄像头总把走廊的人当成家里人」「能不能告诉它忽略窗外的马路」等，用「感知须知」给该机位补指导；还在用户提**画面构图**类诉求时激活——「这台别裁了 / 只看整个画面 / 别老对着一块地方」「这台小目标看不清 / 远处的东西看不清」「智能裁切给这台关了 / 打开」等，用「智能裁切增强」（Smart Crop：裁主体 vs 送整幅）逐机位调。注意区分：开关摄像头设备本身（开机/关机/电源/录制）走 miloco-devices；感知引擎自身的开关/参数（分辨率档 / 全局裁切闸 / 感知窗口大小等）**没有 skill 覆盖**，miloco-perception 只有 perceive devices / logs / query / clear 四条命令（前三条是查询，clear 是清空感知流缓冲，都不是配置/开关），别往那儿转——正确入口（网页「设置」或 miloco-cli config set）与全局闸和逐机位闸的关系，见本 skill 正文。
metadata:
  author: miloco
  version: "2.4"
  date: "2026-08-28"
  openclaw:
    requires:
      bins: ["miloco-cli"]
---

控制 miloco 接入哪些家庭和哪些摄像头。

## 工作方式

- **家庭**：登录后自动启用首个家庭（按 home_id 字典序兜底），多家庭账号可通过 `scope home switch <id>` 切换。同时只能启用**一个**家庭，切换时其余自动停用。
- **摄像头（视频感知）**：默认全部启用。`scope camera disable <did>` 停用感知、`scope camera enable <did>` 恢复。新增摄像头默认接入。
- **摄像头声音（是否参与感知）**：**默认关闭**（opt-in，用户按场景显式开启）。`scope camera mic-on <did>` 开启——该相机声音开始参与感知（识别语音指令、理解环境声）；`mic-off` 关闭后声音**完全不被处理**（不识别、不理解、不上云、听不到语音指令），视频照常感知。从属于视频感知：感知已 disable 的相机不能设声音。默认关的原因是当前远场拾音质量不稳、嘈杂环境易误报。
- 声音开关的定位是「**每摄像头的信噪比开关**」——安静房间（书房 / 卧室）开、嘈杂位（对着电视 / 街边窗口）保持关；用户抱怨某摄像头声音类**误报**是典型触发词（多半是嘈杂位被开了声音，建议关）。
- **摄像头感知须知（每台专属 prompt）**：给某台摄像头补一段自定义指导——机位环境描述、要**关注**的东西、要**忽略**的东西。逐感知窗注入该相机的视觉感知，指导模型消解**固定误识**。`scope camera prompt-set <did> "<文本>"` 设置、`prompt-clear <did>` 清除。默认无。与视频 / 声音开关**正交**：不从属感知开关，相机关着也能预配，只在被感知时生效；改动下一窗即生效、不重启。上限 500 字。典型用途：门口机位能看到公共走廊的电梯门，模型误把电梯门开合当自家入户门 → 用须知说明「画面右侧是公共走廊电梯门，与本户无关，只有正中木色入户门才是本户」。

- **摄像头智能裁切增强（Smart Crop，逐机位）**：**默认开启**。开启时 miloco 会在该机位画面里裁出主体区域再送模型，等效提高主体分辨率、小目标看得更清；关掉即该路改送整幅画面（分辨率档不变）。`scope camera crop-off <did>` 关、`crop-on <did>` 开回默认。逐路可配（`did:chN`，同 prompt-set），因为裁不裁取决于该路镜头的视野。与视频 / 声音 / 须知开关**正交**，不从属感知开关（相机关着也能预配），改动下一窗即生效、不重启。另有一对**全局**开关（`crop_enhance.enabled` / `user_enabled`）与之**相与**：全局关时逐机位设了也不生效。这对开关**不在本 skill、也不在 miloco-miot-admin**（后者只覆盖 `status` / `home-info` / `cost`）——`user_enabled` 在**网页「设置」里的「智能裁切增强」开关**（拨 UI 走 admin API、热更、下一窗生效）。`enabled` 是发版级开关，**admin API 不写它**，随包 `settings.yaml` 发布。

  用 CLI 改这两个都要注意两件事：① `config set` 写的是本机 config.json，**`enabled` 也在 CLI 白名单里**——哪台机器执行过 `config set perception.engine.crop_enhance.enabled`，之后就固定读 config.json，后续发版改 yaml 对这台机器不再生效；② `config set` **默认会顺手重启后端**（因为后端 `get_settings()` 有进程级缓存，CLI 只落盘清不掉），显式传 `--no-restart` 时改动对运行中的后端等于没生效——正是「以为已经关掉了、实际还在裁」那种失效态。用户反馈「逐机位关了没效果 / 开了没效果」时，先用 `scope camera list` 定位**配置层**。`crop_effective=false` 时按这三种情形反查：`in_use=false` = 这一路没进感知采集，**先别急着 `scope camera enable`**——`in_use` 是一串条件的合取（不在黑名单 **且** 家庭已启用 **且** `online and lan_online` **且** 该路镜头未关 **且** 没被 `MAX_ENABLED_CAMERAS=4` 按合成 did 升序截掉），`enable` 只动"不在黑名单"这一项。先看同一行的 `is_online`（云端在线 且 局域网可达）/ `connected`（流已订阅）/ `awake`（该路镜头开关：`true` 开 / `false` 关（隐私遮挡）/ `null` 该机型无此属性或读取失败）：离线、不在同一局域网、镜头被物理关闭、或已超出 4 台名额时，这台本来就不在黑名单里，`enable` 会因"集合无变化"直接短路、连 KV 都不写，接口照样返回成功但 `in_use` 不会变——此时该报的是真实原因，不是重试 `enable`；`in_use=true` 且 `crop_in_use=false` = 这一路自己关了裁切；`in_use=true` 且 `crop_in_use=true` = 被全局闸挡住（逐机位怎么设都是白设），引导用户去网页「设置」开全局开关，**不要**转给 miloco-miot-admin。

  **若 `crop_effective=true` 而用户仍反馈「小目标看不清 / 开了没效果」，先看同一行的 `connected`。** `crop_effective` 只合了「被选中 + 全局双闸 + 本路偏好」，**不含**「流是否真订阅上」：`connected=false` 时这一路根本没进感知窗、裁切判定一次都没被调用，`grep adaptive_crop_fallback` 会是 0 命中——该报的是拉流未建立，别当成内容层回退。`connected=true` 才轮到下面这一层。

  **`connected` 也正常、仍反馈没效果，那就不是配置问题，别停在这里。** 该字段只表示三道闸全开，**不表示每一窗真的裁了**：闸开之后还有**逐窗的内容层判定**会回退全景——裁切区域（窗口内**主体检测框**与**帧差分运动块**两者的并集，再做扩展与最小面积放大）本窗**无检测框且无显著运动块**（`reason=no_activity`，空房间 / 静止画面下最常见的一条，属正常回退、无需处置）、区域面积超上限 `crop_max_area_ratio=0.49` 或不足下限 `crop_min_area_ratio=0.10`、区域退化、本窗无帧、编码或 JPEG 产物过短等。**这里只是举例，完整清单（11 项及各自的 `reason=` 取值）在 `omni/prompt_builder._maybe_encode_adaptive` 的 docstring**，以那份为准，别把本段当全集。**广角机位尤其容易撞上上限**：一个人在 4 秒窗里横穿客厅，并集就可能越过半个画面；**即使画面里只有一个人不动**，电视亮着 / 窗帘飘动 / 摄像头轻微位移产生的运动块也会把并集撑开——所以别用"画面里就一个人、并集不可能超过 0.49"来排除这条。这一层 `scope camera list` 完全看不出来，只能看后端日志：`miloco-cli service logs -n 200 | grep adaptive_crop_fallback`（全仓只有 `service logs` 能读后端应用日志——**不是** `perceive logs`，那个查的是结构化感知事件流；也**不在** miloco-miot-admin 名下）。原因看 `reason=`；`reason=area_too_large` 那行还会带出 `union=` 框，是唯一能看出"谁把并集撑开了"的线索。

所有子命令未知 did/id 均被拒绝（防 typo）。先 `list` 确认合法再操作。

## 何时激活 vs 走别的 skill

- 「感知 X 摄像头」「让 miloco 接入 X 家庭」「只用 / 不用某个目标」「哪些家庭在用」= 控制 miloco 的**感知范围** → 本 skill
- 「关闭感知」「打开感知」「感知开关」= 先问清指的是哪一层，**没有"感知引擎总开关"这种命令**：想让某台相机不被感知 → 本 skill 的 `scope camera disable/enable`；想停掉整个后端 → `miloco-cli service stop/start`。miloco-perception **不能**做这两件事，它只有 `perceive devices` / `logs` / `query` / `clear` 四条命令（均不能开关感知引擎），别往那儿转
- 「调感知参数」（分辨率档 / 全局裁切闸 / 感知窗口大小等）= **没有 skill 覆盖**：miloco-perception 只有 `perceive devices` / `perceive logs` / `perceive query` / `perceive clear` 四条命令（前三条查询，`clear` 清空感知流缓冲），**没有任何配置/参数命令**。直接引导用户去网页「设置」，或给出 `miloco-cli config set perception.<配置路径> <值>`（注意它默认会重启后端，见本文档裁切段落的说明）
- 切设备属性 / 调动作 → miloco-devices
- 刷新设备 / 摄像头列表缓存 → miloco-devices（`miloco-cli device refresh`）。miloco-miot-admin 的 `home-info` 只是读一遍打计数，**不刷新**
- 「清一下感知缓存 / 感知好像卡住了」→ miloco-perception 的 `miloco-cli perceive clear`（POST，清空所有感知设备的流缓冲区）。**不是**配置项，别引到网页「设置」或 `config set`
- 看后端日志（含 `event=adaptive_crop_fallback` 这类感知内部事件）= **没有 skill 覆盖**：直接给 `miloco-cli service logs -n 200`（持续跟踪加 `-f`），全仓只有这一条能读后端应用日志。miloco-miot-admin **不能**做这件事（只有 `status` / `home-info` / `cost`）；`perceive logs` 查的是结构化感知事件流、grep 不到 `adaptive_crop_fallback`，也别往那儿转

### ⚠️ 摄像头：开关「设备」≠ 关闭「感知」

这是最易混的一组，务必按用户原话区分：

- **「打开 / 关闭某摄像头」「把摄像头开机 / 关机 / 断电」「让摄像头别录了」= 控制摄像头设备本身**（开关 / 电源属性）→ **miloco-devices**，不是本 skill。这会真的改变设备状态。
- **「关闭某摄像头的感知」「别让 miloco 看 / 分析这台摄像头」「把这台摄像头从感知里去掉」= 仅停止 miloco 接入其画面**，设备照常运行 → 本 skill 的 `scope camera disable`。
- **「把某摄像头声音关了 / 别听这台的声音」「客厅电视老误报，把客厅摄像头声音关了」「次卧很安静，把声音打开」= 只切声音是否参与感知**，画面照常 → 本 skill 的 `scope camera mic-off / mic-on`。
- **「门口摄像头老把电梯门当成我家门」「这台总把走廊路人当成家里人」「让它忽略窗外的马路 / 电视画面里的人」= 该机位有固定的画面误识，要给它补指导**，不是关掉它 → 本 skill 的 `scope camera prompt-set`（写「感知须知」，见下文流程）。

判据（四路分流）：
- 用户想改变**摄像头设备的状态**（开机/断电/录制）→ IoT 控制（miloco-devices）。
- 用户想改变 **miloco 看不看它**（视频感知范围）→ `scope camera enable/disable`。
- 用户想改变 **miloco 听不听它**（声音，是否参与感知）→ `scope camera mic-on/mic-off`。
- 用户想让 miloco **看得更准**（某机位画面/视觉类固定误识，要补环境说明 / 关注 / 忽略）→ `scope camera prompt-set`。
- 用户想改变某机位**送模型的画面构图**（「这台别裁了 / 只看整个画面」「这台小目标看不清」）→ `scope camera crop-off/crop-on`。注意这**不是**改分辨率档（`video_short_edge`，管多清晰；裁不裁与多清晰正交）——分辨率档和上面那对全局裁切闸在同一处：网页「设置」或 `miloco-cli config set perception.engine.input.video_short_edge <值>`。它**不在** miloco-miot-admin（只有 status / home-info / cost）；上面那条「调感知参数」的路由也覆盖不到它（miloco-perception 只有 `perceive devices` / `logs` / `query` / `clear` 四条命令，**没有任何配置命令**——不是缺分辨率档这一条，是整个配置面都不在那儿），所以别转给任何 skill，直接按上面这一处引导用户。也不是改看不看它。
- 拿不准时按字面：「感知 / 接入 / 别看 / 别分析」→ 视频范围；「声音 / 别听 / 声音误报」→ 声音开关；「误认 / 认成 / 当成 / 老把 X 当 Y / 忽略画面里的 Z」这类**画面识别错误**→ 感知须知。

### 声音类误报 vs 画面类误识（别混）

用户说「误报 / 误识」时先分清是哪一类：
- **声音类**（听错：把电视声当人说话、嘈杂环境误触发语音指令）→ `mic-off` 关这台声音。
- **画面类**（看错：把电梯门当自家门、把走廊路人当家人、被窗外马路 / 电视里的人干扰）→ `prompt-set` 写须知补指导，**不要**关摄像头/关声音（那样会丢掉这台的正常感知）。

## 命令

```
miloco-cli scope home   list | switch <id>
miloco-cli scope camera list | enable <did>... | disable <did>...
miloco-cli scope camera mic-on <did>... | mic-off <did>...
miloco-cli scope camera prompt-set <did> "<文本>" | prompt-clear <did>...
miloco-cli scope camera crop-on <did>... | crop-off <did>...
```

- **家庭 `switch <id>`**：切换到该家庭（唯一启用），其余自动停用。只接受 1 个 id。
- **摄像头 `enable/disable <did>...`**：视频感知批量启用/停用，可同时操作多个 did。
- **摄像头 `mic-on/mic-off <did>...`**：声音批量开/关，同款批量 did 语义。`mic-off` = 该相机声音完全不被处理；仅感知已启用(in_use=true)的相机可设，感知已关闭时整批被拒。改动即时生效、无需重启。
- **摄像头 `prompt-set <did> "<文本>"`**：给该机位设自定义「感知须知」（**文本务必加引号**）。`prompt-clear <did>...` 清除（可批量）。上限 500 字，超限被后端拒。与启用/声音开关正交，不从属感知，改动下一窗即生效、不重启。
- **摄像头 `crop-on/crop-off <did>...`**：智能裁切增强批量开/关，同款批量 did 语义，**精确到路**（`did:chN`；裸多通道 did = 该台全部通道）。`crop-off` = 该路改送整幅画面。不校验 in_use（关着的相机也可预配）。与全局双闸相与，全局关时不生效但**不报错**。改动下一窗即生效、不重启。
- `list` 输出每项含 `in_use`（是否启用）；camera 额外带 `is_online`（设备在线）、`connected`（流已订阅）、`awake`（该路镜头开关，`null`=机型无此属性或读取失败）、`voice_in_use`（声音开关）、`crop_in_use`（该路智能裁切**存储偏好**，默认 true）、`crop_effective`（智能裁切**生效态** = `in_use` AND 全局双闸 AND `crop_in_use`）和 `perception_prompt`（该机位自定义感知须知）。`in_use`/`is_online`/`connected` 三者都 true = 正常采集，任一 false 即某层未就位。`voice_in_use=false` = 该相机声音完全不被处理（不转写、不上云、听不到指令），视频照常感知。

## "只用 X" 模式

- **家庭**：`scope home switch <id>` 直接切换，其余自动停用。
- **摄像头**：`scope camera disable <其它所有 did>` 停用不需要的。
- 恢复某个被停用的目标 → `scope home switch <id>` / `scope camera enable <did>`。

## 校验行为

| 操作 | 校验规则 |
| --- | --- |
| **家庭 switch** | **拒绝**未知 home_id（切到不存在的家庭无意义） |
| **摄像头 enable** | **拒绝**未知 did |
| **摄像头 disable** | **拒绝**未知 did |
| **摄像头 mic-on/mic-off** | **拒绝**未知 did；**拒绝**感知已关闭(in_use=false)的相机（声音从属于视频感知，先 `enable` 再设声音） |
| **摄像头 prompt-set/prompt-clear** | **拒绝**未知 did；**拒绝**超 500 字。不校验 in_use（关着的相机也可预配须知） |
| **摄像头 crop-on/crop-off** | **拒绝**未知 did；**拒绝**越界通道号（`:chN` 超出该台通道数）。不校验 in_use，也不校验全局双闸（全局关时可设但不生效） |

未知 id / 从属违规由 backend 拒绝并返回错误，CLI 透传错误信息。若不确定 id 合法性，先 `scope home list` / `scope camera list` 看一眼。

## 处理画面类误识：写「感知须知」的流程

用户吐槽某摄像头有**固定的画面误识**（老把 X 当 Y、被某类东西干扰）时，别急着写，按这个闭环走：

1. **定位是哪台、错在哪。** 先 `scope camera list`，按 `name`/`room_name` 找到那台的 `did`。若用户描述模糊，读该 did 的当日感知日志（`memory/YYYY-MM-DD-miloco-perception.md`，或用 memory_search）确认误识的具体表现：把什么当成了什么、什么时候、画面里的位置。
2. **起草须知（环境 + 关注 + 忽略）。** 一段自然语言即可，讲清三件事：① 这台机位**看到的环境**（装在哪、画面里有什么）；② 要**关注**的（哪些才是本户/本场景的真实事件）；③ 要**忽略**的（哪些是干扰、与本户无关）。针对性写、别写成通用套话——越贴合这台画面越有效。
3. **先复述给用户确认再写。** 把你要写的须知念给用户听，让他补充/纠正（尤其"哪个才是自家的门/人"这类只有住户知道的事实）。确认后再执行。
4. **写入并告知生效方式。** `scope camera prompt-set <did> "<确认后的文本>"`，然后告诉用户：下一个感知窗即生效、无需重启；若之后仍有误识，可以继续追加/调整须知，或让你 `prompt-clear` 重来。
5. **已有须知时是追加不是覆盖。** `prompt-set` 是整段覆盖。若这台已配过（`perception_prompt` 非空），先读出旧文本，在其上增补后整段写回，别把用户之前的指导冲掉。

不要用须知去表达"关掉这台/关声音"——那是 `disable`/`mic-off` 的活；须知只用来**让保留的感知看得更准**。

## 状态字段与时序

- `is_online=false` = 设备 / 网络层问题，不在本 skill 范围；让用户检查设备本身。
- `connected=false` 且 `in_use=true && is_online=true` = 接入配置已就绪但流还没拉起来。等一个 `sync_devices()` 周期；若过了周期仍不连，问题不在接入配置，走 miloco-perception。
- 修改即时生效：CLI 写完配置后后端 `sync_devices()` 热同步，无需重启服务。

## 示例

```
# 查看接入状态（list 返回 {code, message, data} 信封）
$ miloco-cli scope home list
  → {"code":0,"message":"ok","data":[
       {"home_id":"611001054724","home_name":"HCl的家","in_use":false},
       {"home_id":"611001866489","home_name":"xiaomi","in_use":true}]}

$ miloco-cli scope camera list
  → {"code":0,"message":"ok","data":[
       {"did":"1154253569","name":"小米智能摄像机C700","is_online":true,"in_use":true,"connected":true}]}

# 切换到 xiaomi 家庭（其余自动停用，返回全量家庭列表）
$ miloco-cli scope home switch 611001866489
  → {"code":0,"message":"ok","data":[
       {"home_id":"611001054724","home_name":"HCl的家","in_use":false},
       {"home_id":"611001866489","home_name":"xiaomi","in_use":true}]}

# 切换到另一个家庭
$ miloco-cli scope home switch 611001054724
  → {"code":0,"message":"ok","data":[
       {"home_id":"611001054724","home_name":"HCl的家","in_use":true},
       {"home_id":"611001866489","home_name":"xiaomi","in_use":false}]}

# 停用一台摄像头（返回操作后的摄像头列表）
$ miloco-cli scope camera list        # 看 did
$ miloco-cli scope camera disable 1154253569
  → {"code":0,"message":"ok","data":[
       {"did":"1154253569","name":"小米智能摄像机C700","is_online":true,"in_use":false,"connected":false}]}

# 恢复被停用的摄像头
$ miloco-cli scope camera enable 1154253569
  → {"code":0,"message":"ok","data":[
       {"did":"1154253569","name":"小米智能摄像机C700","is_online":true,"in_use":true,"connected":true}]}

# 「客厅电视老误报，把客厅摄像头声音关了」——关声音（视频照常感知）
$ miloco-cli scope camera list        # 按 room/name 找到客厅摄像头 did
$ miloco-cli scope camera mic-off 1154253569
  → {"code":0,"message":"ok","data":[
       {"did":"1154253569","name":"小米智能摄像机C700","is_online":true,"in_use":true,"voice_in_use":false,"connected":true}]}

# 「次卧很安静，把声音打开」——开声音
$ miloco-cli scope camera mic-on 1154253570
  → {"code":0,"message":"ok","data":[
       {"did":"1154253570","name":"小米智能摄像机C700","is_online":true,"in_use":true,"voice_in_use":true,"connected":true}]}

# 「门口摄像头老把电梯门开了当成我家门开了」——画面类误识，写感知须知（不是关摄像头）
$ miloco-cli scope camera list        # 按 room/name 找到门口那台 did
# （先复述须知给用户确认："画面右侧公共走廊里的电梯门，与本户无关……"，确认后再写）
$ miloco-cli scope camera crop-off <did>:ch0        # 关掉某一路的智能裁切
$ miloco-cli scope camera crop-on  <did>            # 裸 did = 该台全部通道，开回默认
$ miloco-cli scope camera list                      # 排查链要读的四个字段：
#   in_use / connected / crop_in_use / crop_effective
#   crop_effective=false 时：in_use=false → 不在感知范围；crop_in_use=false → 这一路自己关的；
#   两者都 true → 被全局闸挡住。crop_effective=true 仍反馈没效果 → 先看 connected，再看后端日志。

$ miloco-cli scope camera prompt-set 1154253571 "本摄像头装在入户门内，画面右侧公共走廊里可见电梯门。电梯门开合与本户无关，不要据此判断有人回家/开门；只有画面正中的木色入户门开合才是本户事件。"
  → {"code":0,"message":"ok","data":[
       {"did":"1154253571","name":"小米智能摄像机C700","is_online":true,"in_use":true,"voice_in_use":false,"perception_prompt":"本摄像头装在入户门内……","connected":true}]}

# 清除某台的感知须知（回到无自定义）
$ miloco-cli scope camera prompt-clear 1154253571
  → {"code":0,"message":"ok","data":[
       {"did":"1154253571","name":"小米智能摄像机C700","is_online":true,"in_use":true,"perception_prompt":"","connected":true}]}
```
