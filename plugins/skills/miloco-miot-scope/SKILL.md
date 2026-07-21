---
name: miloco-miot-scope
description: 感知范围控制 — 管理 miloco 感知哪些家庭、哪些摄像头，每台摄像头的声音（是否参与感知），以及每台摄像头的「感知须知」（专属 prompt，补机位环境 / 关注 / 忽略以提升感知准确性）。用户说「只用/不用某家庭」「让 miloco 感知/别感知 某家庭或摄像头」「屏蔽某摄像头/把某摄像头从感知里去掉」「把某摄像头声音关了/打开」「XX 老误报（声音类）」「哪些家庭在用」时激活；也在用户吐槽**画面/视觉类固定误识**时激活——「门口摄像头老把电梯门当成我家门」「XX 老把 Y 认成 Z」「这台摄像头总把走廊的人当成家里人」「能不能告诉它忽略窗外的马路」等，用「感知须知」给该机位补指导。注意区分：开关摄像头设备本身（开机/关机/电源/录制）走 miloco-devices；感知引擎自身的开关/参数走 miloco-perception。
metadata:
  author: miloco
  version: "2.3"
  date: "2026-07-14"
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

所有子命令未知 did/id 均被拒绝（防 typo）。先 `list` 确认合法再操作。

## 何时激活 vs 走别的 skill

- 「感知 X 摄像头」「让 miloco 接入 X 家庭」「只用 / 不用某个目标」「哪些家庭在用」= 控制 miloco 的**感知范围** → 本 skill
- 「关闭感知」「打开感知」「感知开关」「调感知参数」= 控制**感知引擎自身** → miloco-perception
- 切设备属性 / 调动作 → miloco-devices
- 刷新缓存 / 看日志 → miloco-miot-admin

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
```

- **家庭 `switch <id>`**：切换到该家庭（唯一启用），其余自动停用。只接受 1 个 id。
- **摄像头 `enable/disable <did>...`**：视频感知批量启用/停用，可同时操作多个 did。
- **摄像头 `mic-on/mic-off <did>...`**：声音批量开/关，同款批量 did 语义。`mic-off` = 该相机声音完全不被处理；仅感知已启用(in_use=true)的相机可设，感知已关闭时整批被拒。改动即时生效、无需重启。
- **摄像头 `prompt-set <did> "<文本>"`**：给该机位设自定义「感知须知」（**文本务必加引号**）。`prompt-clear <did>...` 清除（可批量）。文本空 = 清除。上限 500 字，超限被后端拒。与启用/声音开关正交，不从属感知，改动下一窗即生效、不重启。
- `list` 输出每项含 `in_use`（是否启用）；camera 额外带 `is_online`（设备在线）、`connected`（流已订阅）、`voice_in_use`（声音开关）和 `perception_prompt`（该机位自定义感知须知，"" = 无）。`in_use`/`is_online`/`connected` 三者都 true = 正常采集，任一 false 即某层未就位。`voice_in_use=false` = 该相机声音完全不被处理（不转写、不上云、听不到指令），视频照常感知。

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
$ miloco-cli scope camera prompt-set 1154253571 "本摄像头装在入户门内，画面右侧公共走廊里可见电梯门。电梯门开合与本户无关，不要据此判断有人回家/开门；只有画面正中的木色入户门开合才是本户事件。"
  → {"code":0,"message":"ok","data":[
       {"did":"1154253571","name":"小米智能摄像机C700","is_online":true,"in_use":true,"voice_in_use":false,"perception_prompt":"本摄像头装在入户门内……","connected":true}]}

# 清除某台的感知须知（回到无自定义）
$ miloco-cli scope camera prompt-clear 1154253571
  → {"code":0,"message":"ok","data":[
       {"did":"1154253571","name":"小米智能摄像机C700","is_online":true,"in_use":true,"perception_prompt":"","connected":true}]}
```
