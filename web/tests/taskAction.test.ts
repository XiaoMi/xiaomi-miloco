/**
 * 任务动作 → 人话。测的是纯函数，不渲 DOM。
 *
 * 重点钉三件事：设备/能力/值的取数口径、解析不出时的回落、冷却与幂等的三态。
 */

import { describe, expect, it } from "vitest";
import type { TaskDeviceMeta } from "@/api/real";
import { describeTaskAction, type ActionCatalog } from "@/lib/taskAction";
import type { TaskAction } from "@/lib/types";

/** i18n 桩：把 key 与插值原样带出，断言时能同时看到「用了哪个 key」和「传了什么参数」。
 *  不走真 i18n 实例——本模块只依赖 key 与插值两件事，翻译文案本身在 locales 里。 */
const t = (key: string, opts?: Record<string, unknown>): string => {
  if (!opts) return key;
  const args = Object.entries(opts)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(",");
  return `${key}(${args})`;
};

function device(over: Partial<TaskDeviceMeta> = {}): TaskDeviceMeta {
  return {
    did: "d1",
    name: "米家智能晾衣机 3 MAX",
    room: "卧室",
    spec: {
      "prop.2.5": {
        description: "motor-control",
        prop_description: "motor-control",
        format: "uint8",
        value_list: [
          { name: "Up", value: 1 },
          { name: "Down", value: 2 },
        ],
      },
      "action.7.3": {
        description: "play-text",
        prop_description: "play-text",
        type_name: "play-text",
        in_params: [{ name: "text", format: "string" }],
      },
      "action.2.1": {
        description: "ptz-calibrate",
        prop_description: "ptz-calibrate",
        type_name: "ptz-calibrate",
      },
      "action.9.1": {
        description: "three-params",
        prop_description: "three-params",
        type_name: "three-params",
        in_params: [
          { name: "mode", format: "string" },
          { name: "text", format: "string" },
          { name: "volume", format: "uint8" },
        ],
      },
      "action.7.4": {
        description: "execute-text-directive",
        prop_description: "execute-text-directive",
        type_name: "execute-text-directive",
        in_params: [
          { name: "text-content", format: "string" },
          { name: "silent-execution", format: "bool" },
        ],
      },
      "action.6.1": {
        // 云端译名的形态：description 是「服务名 动作名」，prop_description
        // 仍是 MiOT 英文 type_name
        description: "音箱 播放文本",
        prop_description: "play-text",
        type_name: "play-text",
        service_description: "音箱",
        in_params: [{ name: "text", format: "string" }],
      },
    },
    ...over,
  };
}

function catalog(over: Partial<ActionCatalog> = {}): ActionCatalog {
  return {
    devices: new Map([["d1", device()]]),
    scenes: new Map([["s1", "回家模式"]]),
    ready: true,
    t,
    ...over,
  };
}

function action(over: Partial<TaskAction> = {}): TaskAction {
  return { did: "d1", iid: "prop.2.5", value: 2, ...over };
}

describe("describeTaskAction", () => {
  it("设备控制：设备名 + 房间 + 能力 + 枚举值", () => {
    const d = describeTaskAction(action(), catalog());
    expect(d.kind).toBe("control");
    expect(d.device).toBe("米家智能晾衣机 3 MAX");
    expect(d.room).toBe("卧室");
    // specLabel 走 zhLabel 词表，词表外回落 prop_description
    expect(d.capability).toBe("motor-control");
    // 值取自 spec 的 value_list；枚举名过 zhEnumValue 词表，词表外原样回退
    expect(d.detail).toBe("Down");
    expect(d.unresolved).toBe(false);
  });

  it("动作调用：播报文本来自 params，多个参数按分隔符拼接", () => {
    const d = describeTaskAction(
      action({ iid: "action.7.3", value: null, params: ["关灯", "客厅"] }),
      catalog(),
    );
    expect(d.kind).toBe("broadcast");
    // 多参数按 in_params 补参数名：第一个参数有名字（text），第二个没有
    expect(d.detail).toBe("text 关灯actions.listSep客厅");
  });

  it("场景：did 位置放的是 scene_id，名字从场景表取", () => {
    const d = describeTaskAction(
      action({ iid: "scene", did: "s1", value: null }),
      catalog(),
    );
    expect(d.kind).toBe("scene");
    expect(d.device).toBe("tasks.sceneTarget(name=回家模式)");
    // 能力名留空：类型徽标已经写了「触发场景」，再填一遍会同一行出现两次
    expect(d.capability).toBe("");
    expect(d.unresolved).toBe(false);
  });

  it("场景行也带幂等标签：后端强制场景动作非幂等", () => {
    const d = describeTaskAction(
      action({ iid: "scene", did: "s1", value: null, idempotent: false, cooldownMinutes: 5 }),
      catalog(),
    );
    expect(d.chips).toContain("tasks.chipNonIdempotent");
    expect(d.chips).toContain("tasks.chipCooldownMinutes(n=5)");
  });

  it("场景查不到 → 显式标未知场景，不把 scene_id 当名字", () => {
    const d = describeTaskAction(
      action({ iid: "scene", did: "nope", value: null }),
      catalog(),
    );
    expect(d.device).toBe("tasks.sceneTargetUnknown");
    expect(d.unresolved).toBe(true);
    expect(d.detail).toBe("nope");
  });

  it("设备不在可见范围（已查过）→ 未知设备 + unresolved", () => {
    const d = describeTaskAction(action({ did: "ghost" }), catalog());
    expect(d.device).toBe("tasks.unknownDevice(did=ghost)");
    expect(d.unresolved).toBe(true);
  });

  it("设备表还没落地 → 原样显示 did，不报「未知设备」", () => {
    const d = describeTaskAction(action({ did: "ghost" }), catalog({ ready: false }));
    expect(d.device).toBe("ghost");
    expect(d.unresolved).toBe(false);
  });

  it("action.* 不都是播报：非文本类设备动作单独归类", () => {
    const d = describeTaskAction(
      action({ iid: "action.2.1", value: null, params: [] }),
      catalog(),
    );
    expect(d.kind).toBe("deviceAction");
  });

  it("中间有空参数时，后面参数的名字不前移", () => {
    const d = describeTaskAction(
      action({ iid: "action.9.1", params: ["", "关灯", 30] }),
      catalog(),
    );
    // 名字按原始下标取：'' 占住 mode 位，「关灯」是 text、30 是 volume
    expect(d.detail).toBe("text 关灯actions.listSepvolume 30");
    expect(d.detail).not.toContain("mode 关灯");
  });

  it("单参数加引号、不贴参数名（与活动流同一个文案键）", () => {
    const d = describeTaskAction(
      action({ iid: "action.7.3", params: ["你好"] }),
      catalog(),
    );
    expect(d.detail).toBe("actions.quoted(v=你好)");
  });

  it("多参数按 in_params 带名，布尔标志不再裸成一个 false", () => {
    const d = describeTaskAction(
      action({ iid: "action.7.4", params: ["打开客厅灯", false] }),
      catalog(),
    );
    expect(d.detail).toContain("text-content 打开客厅灯");
    expect(d.detail).toContain("silent-execution false");
  });

  it("execute-text-directive 不算播报：它是让小爱执行的指令，不保证出声", () => {
    const d = describeTaskAction(
      action({ iid: "action.7.4", value: null, params: ["打开客厅灯"] }),
      catalog(),
    );
    expect(d.kind).toBe("deviceAction");
  });

  it("spec 缺字段时按设备动作兜底，不谎称在说话", () => {
    const d = describeTaskAction(
      action({ iid: "action.9.9", value: null, params: [] }),
      catalog(),
    );
    expect(d.kind).toBe("deviceAction");
  });

  // 前向兜底：后端目前不产出 {{result}}（见 RESULT_PLACEHOLDER 的注释）。
  // 这条只钉「若将来接入回填，展示层不把模板裸露给住户」，不代表该链路已存在。
  it("生成式动作的 {{result}} 占位符不裸展示（前向兜底，后端暂未产出）", () => {
    const d = describeTaskAction(
      action({ iid: "action.7.3", params: ["{{result}}"] }),
      catalog(),
    );
    expect(d.detail).toBe("tasks.generatedByOmni");
  });

  it("普通文案里的花括号不被误判成占位符", () => {
    const d = describeTaskAction(
      action({ iid: "action.7.3", params: ["温度 {{x}} 已到"] }),
      catalog(),
    );
    expect(d.detail).toBe("actions.quoted(v=温度 {{x}} 已到)");
  });

  it("冷却：只认后端产出的分钟级字段", () => {
    const min = describeTaskAction(action({ cooldownMinutes: 3 }), catalog());
    expect(min.chips).toContain("tasks.chipCooldownMinutes(n=3)");

    // 未配置就不显示——「没写」不是一种冷却状态
    expect(describeTaskAction(action(), catalog()).chips).toEqual([]);
  });

  it("幂等：只在显式非幂等时提示", () => {
    // 后端 RuleAction.idempotent 默认 true，透传到 task 槽时会被实体化——拿到的
    // true 分不清「显式声明」与「没写」，逐行挂一枚「幂等」等于没说
    expect(describeTaskAction(action({ idempotent: true }), catalog()).chips).toEqual([]);
    expect(
      describeTaskAction(action({ idempotent: false }), catalog()).chips,
    ).toEqual(["tasks.chipNonIdempotent"]);
    expect(describeTaskAction(action(), catalog()).chips).toEqual([]);
  });

  it("动作的能力名取云端译名，不把 MiOT 英文 type_name 露给住户", () => {
    const d = describeTaskAction(
      action({ iid: "action.6.1", value: null, params: ["关灯"] }),
      catalog(),
    );
    // description 是「服务名 动作名」，设备名里已经含服务名，只留动作名
    expect(d.capability).toBe("播放文本");
    expect(d.capability).not.toContain("play-text");
  });

  it("老 spec 缺 type_name 时，prop_description 归一化后仍判为播报", () => {
    // prop_description 是英文人类可读名（Play Text），不是 URN 里的 play-text；
    // 不归一化会被误判成设备动作
    const dev = device();
    dev.spec = {
      ...(dev.spec ?? {}),
      "action.8.1": { prop_description: "Play Text" },
    };
    const d = describeTaskAction(
      action({ iid: "action.8.1", value: null, params: ["关灯"] }),
      catalog({ devices: new Map([["d1", dev]]) }),
    );
    expect(d.kind).toBe("broadcast");
  });

  it("动作没有云端译名时回落到 specLabel", () => {
    const dev = device();
    dev.spec = {
      ...(dev.spec ?? {}),
      "action.2.1": {
        prop_description: "ptz-calibrate",
        type_name: "ptz-calibrate",
      },
    };
    const d = describeTaskAction(
      action({ iid: "action.2.1", value: null, params: [] }),
      catalog({ devices: new Map([["d1", dev]]) }),
    );
    expect(d.capability).toBe("ptz-calibrate");
  });

  it("params 不是数组时按单参数渲染，不抛异常", () => {
    // task 边界槽在后端是 list[dict]，params 写成字符串 / 对象都能落库
    const asText = describeTaskAction(
      action({ iid: "action.7.3", value: null, params: "晚安" }),
      catalog(),
    );
    expect(asText.detail).toBe("actions.quoted(v=晚安)");
    const asObject = describeTaskAction(
      action({ iid: "action.7.3", value: null, params: { text: "晚安" } }),
      catalog(),
    );
    expect(asObject.detail).toBe('actions.quoted(v={"text":"晚安"})');
  });

  it("多入参动作只填了一个非空值时仍带参数名", () => {
    const d = describeTaskAction(
      action({ iid: "action.7.4", params: ["", false] }),
      catalog(),
    );
    // 声明了两个入参，false 是 silent-execution，不能裸成一个 false
    expect(d.detail).toBe("silent-execution false");
  });

  it("单入参动作存了空位时，幸存值不带名字", () => {
    const d = describeTaskAction(
      action({ iid: "action.7.3", params: ["", "你好"] }),
      catalog(),
    );
    // 原始 params 有两个、走多入参分支；names[1] 不存在，所以只给原文不加名
    expect(d.detail).toBe("你好");
  });

  it("属性值不是标量时按 JSON 显示，不压成 [object Object]", () => {
    // 与 params 那条同口径：同一个边界槽里的两种形状不该有两种回落
    const d = describeTaskAction(
      action({ iid: "prop.9.9", value: { level: 3 } }),
      catalog(),
    );
    expect(d.detail).toBe('{"level":3}');
  });

  it("spec 缺失时能力回落成 iid，值回落成原始字面量", () => {
    const d = describeTaskAction(
      action({ iid: "prop.9.9", value: "abc" }),
      catalog(),
    );
    expect(d.capability).toBe("prop.9.9");
    expect(d.detail).toBe("abc");
  });
});
