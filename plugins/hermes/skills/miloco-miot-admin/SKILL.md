---
name: miloco-miot-admin
description: Miloco 系统运维：连通性 / 设备缓存 / 感知成本统计。
metadata:
  author: miloco
  version: "1.3"
  date: "2026-06-17"
  openclaw:
    requires:
      bins: ["miloco-cli"]
---

# miloco-miot-admin

Miloco 系统运维 Skill。覆盖 `miloco-cli admin`：`status` / `home-info` / `cost`。

## 何时激活

| 意图          | 用户说了类似…                                  | 命令                  |
| ------------- | ---------------------------------------------- | --------------------- |
| **status**    | "Miloco 状态怎么样" "系统正常吗" "连接状态"   | `admin status`        |
| **home_info** | "设备缓存什么时候更新的" "缓存还有效吗"       | `admin home-info`     |
| **cost**      | "今天花了多少" "感知调用花了多少钱"           | `admin cost`          |

**判断规则**：用户问的是系统本身的状态/缓存/成本，而非设备控制（→ miloco-devices）或环境感知（→ miloco-perception）→ 激活本 skill。

## 命令对照

| 操作         | 命令                              | 说明                                           |
| ------------ | --------------------------------- | ---------------------------------------------- |
| 系统状态     | `miloco-cli admin status`         | MiOT 连接、SQLite、感知模型、规则引擎连通性    |
| 缓存状态     | `miloco-cli admin home-info`      | 从后端拉取设备/区域/场景/成员数量摘要（走 HTTP） |
| 感知成本     | `miloco-cli admin cost --period {today\|month}` | LLM 调用次数与费用（**当前返回 501，未实现**） |

> `home-info` 从后端拉取设备/区域/场景/成员数量摘要。

## 异常处理

| 异常        | 处理                            | 回复                              |
| ----------- | ------------------------------- | --------------------------------- |
| CLI 不可用  | 提示检查 miloco-cli 安装        | "miloco-cli 未响应，请检查安装"   |
| 服务不可达  | 报告具体不可达的服务            | "MiOT 连接异常，其他服务正常"     |
| cost 未实现 | 直接告知                        | "成本统计功能暂未上线"            |

## 关键规则

1. **系统级 Skill。** 覆盖运维查询（状态/缓存/成本）；不涉及设备控制 / 环境感知 / 模型切换——`admin` 没有 model 子命令。
2. **`cost` 永远先告知未实现。** 当前固定返回 501，不要解析 stdout 里的数字。

## 示例

### 系统状态 — "Miloco 状态怎么样"

```
$ miloco-cli admin status
  → { "miot": "connected", "perception": "running", "sqlite": "ok", "omni_api": "ok" }
回复："系统正常，所有服务已连接"
```

### 缓存状态 — "设备信息什么时候更新的"

```
$ miloco-cli admin home-info
  → { "devices": 23, "areas": 5, "scenes": 3, "persons": 4 }
回复："当前 23 个设备，5 个区域，3 个场景，4 个家庭成员"
```

### 成本统计 — "今天花了多少"

```
$ miloco-cli admin cost --period today
  → { "code": 501, "message": "cost statistics not yet supported" }
回复："成本统计功能暂未上线"
```

## 边界

- ❌ **不切换/管理感知模型**——CLI 当前没有 `admin model` 命令，agent 不得编造。涉及模型切换需在服务端配置。
- ❌ 不控制设备（→ miloco-devices）
- ❌ 不做环境感知（→ miloco-perception）
- ❌ 不管理接入范围（家庭 / 相机）（→ miloco-miot-scope）
- ❌ 无 perception 启停 / log / storage 命令
- ✅ 多数 admin 命令本地执行；`home-info` 走后端 HTTP
