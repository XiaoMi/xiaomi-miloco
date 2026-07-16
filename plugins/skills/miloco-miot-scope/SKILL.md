---
name: miloco-miot-scope
description: 感知范围控制 — 管理 miloco 感知哪些家庭、哪些摄像头，以及每台摄像头的声音（是否参与感知）。用户说「只用/不用某家庭」「让 miloco 感知/别感知 某家庭或摄像头」「屏蔽某摄像头/把某摄像头从感知里去掉」「把某摄像头声音关了/打开」「XX 老误报（声音类）」「哪些家庭在用」时激活。注意区分：开关摄像头设备本身（开机/关机/电源/录制）走 miloco-devices；感知引擎自身的开关/参数走 miloco-perception。
metadata:
  author: miloco
  version: "2.2"
  date: "2026-07-09"
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

判据（三路分流）：
- 用户想改变**摄像头设备的状态**（开机/断电/录制）→ IoT 控制（miloco-devices）。
- 用户想改变 **miloco 看不看它**（视频感知范围）→ `scope camera enable/disable`。
- 用户想改变 **miloco 听不听它**（声音，是否参与感知）→ `scope camera mic-on/mic-off`。
- 拿不准时按字面：「感知 / 接入 / 别看 / 别分析」→ 视频范围；「声音 / 别听 / 声音误报」→ 声音开关。

## 命令

```
miloco-cli scope home   list | switch <id>
miloco-cli scope camera list | enable <did>... | disable <did>...
miloco-cli scope camera mic-on <did>... | mic-off <did>...
```

- **家庭 `switch <id>`**：切换到该家庭（唯一启用），其余自动停用。只接受 1 个 id。
- **摄像头 `enable/disable <did>...`**：视频感知批量启用/停用，可同时操作多个 did。
- **摄像头 `mic-on/mic-off <did>...`**：声音批量开/关，同款批量 did 语义。`mic-off` = 该相机声音完全不被处理；仅感知已启用(in_use=true)的相机可设，感知已关闭时整批被拒。改动即时生效、无需重启。
- `list` 输出每项含 `in_use`（是否启用）；camera 额外带 `is_online`（设备在线）、`connected`（流已订阅）和 `voice_in_use`（声音开关）。`in_use`/`is_online`/`connected` 三者都 true = 正常采集，任一 false 即某层未就位。`voice_in_use=false` = 该相机声音完全不被处理（不转写、不上云、听不到指令），视频照常感知。

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

未知 id / 从属违规由 backend 拒绝并返回错误，CLI 透传错误信息。若不确定 id 合法性，先 `scope home list` / `scope camera list` 看一眼。

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
```
