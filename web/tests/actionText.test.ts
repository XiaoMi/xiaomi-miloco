/**
 * 动作台账行 → 人话的转换。node 环境无 jsdom，故本文件只覆盖文本层——
 * 「动作行渲染成什么样」不在覆盖范围内（那是 ActionRow 的 DOM 结构）。
 *
 * 覆盖三件事：按 action_type 分派取数、spec 词表 / format 读值，以及**回落**。
 * 回落是本模块一半的价值：读不出人话必须返回 null（调用方退回原始键），
 * 猜一个像样的名字比原始键更糟——原始键至少能被搜到、被复制去查。
 *
 * 断言用真实 i18n 实例取标点（顿号 / 引号），因此本文件顺带守：
 * 这几个 key 在 zh 里存在且取值未变（zh/en 对齐另有 i18n.test.ts 守）。
 */
import { describe, it, expect } from "vitest";
import i18n from "@/i18n";
import { describeAction } from "@/lib/actionText";
import type { BackendPropSpec } from "@/api/real";

const t = i18n.t.bind(i18n);

/** 一份小规格表，刻意混入三类名字来源：
 *  `prop_description` 命中词表（Mode → 模式）、未命中词表（Switch / Play Text，
 *  按设计原样透出英文）、只有 `description`（云端译名）。 */
const spec: Record<string, BackendPropSpec> = {
  "prop.2.1": { prop_description: "Switch", format: "bool" },
  "prop.3.1": { prop_description: "Temperature", format: "float", unit: "celsius" },
  "prop.3.2": {
    prop_description: "Mode",
    format: "uint8",
    value_list: [
      { name: "Auto", value: 0 },
      { name: "Sleep", value: 1 },
    ],
  },
  "action.5.1": {
    prop_description: "Play Text",
    in_params: [{ name: "text", format: "string" }],
  },
  "action.6.1": {
    description: "窗帘 开到百分比",
    in_params: [
      { name: "percent", format: "uint8" },
      { name: "speed", format: "uint8" },
    ],
  },
};

describe("describeAction · 单属性（set_property）", () => {
  it("bool 读成开/关，名字走词表外的原名", () => {
    expect(
      describeAction(
        { action_type: "set_property", iid: "prop.2.1", value_json: "true" },
        spec,
        t,
      ),
    ).toBe("Switch 开");
  });

  it("数值拼单位（value_json 是裸标量，不是 {iid: value}）", () => {
    expect(
      describeAction(
        { action_type: "set_property", iid: "prop.3.1", value_json: "26.5" },
        spec,
        t,
      ),
    ).toBe("温度 26.5°C");
  });

  it("枚举按 value_list 里的名走同一张枚举词表", () => {
    expect(
      describeAction(
        { action_type: "set_property", iid: "prop.3.2", value_json: "1" },
        spec,
        t,
      ),
    ).toBe("模式 睡眠");
  });
});

describe("describeAction · 多属性（set_properties）", () => {
  it("逗号拼接的 iid 逐个成句，按当前语言的顿号相接", () => {
    expect(
      describeAction(
        {
          action_type: "set_properties",
          iid: "prop.2.1,prop.3.1",
          value_json: '{"prop.2.1":true,"prop.3.1":26}',
        },
        spec,
        t,
      ),
    ).toBe("Switch 开、温度 26°C");
  });

  it("有一个属性翻不出来就整行回落，不画半人半机器的行", () => {
    expect(
      describeAction(
        {
          action_type: "set_properties",
          iid: "prop.2.1,prop.9.9",
          value_json: '{"prop.2.1":true,"prop.9.9":1}',
        },
        spec,
        t,
      ),
    ).toBeNull();
  });
});

describe("describeAction · 调用方法（call_action）", () => {
  it("单个入参加引号、不贴参数名（参数名对住户无意义，引号已说明是原文）", () => {
    expect(
      describeAction(
        {
          action_type: "call_action",
          iid: "action.5.1",
          value_json: '["厨房检测到烟雾，燃气阀已关闭"]',
        },
        spec,
        t,
      ),
    ).toBe("Play Text 「厨房检测到烟雾，燃气阀已关闭」");
  });

  it("无入参时只给名字", () => {
    expect(
      describeAction(
        { action_type: "call_action", iid: "action.5.1", value_json: "[]" },
        spec,
        t,
      ),
    ).toBe("Play Text");
  });

  it("多个入参逐个贴名，名字取 spec 的 in_params", () => {
    expect(
      describeAction(
        { action_type: "call_action", iid: "action.6.1", value_json: "[50,3]" },
        spec,
        t,
      ),
    ).toBe("窗帘 开到百分比 percent 50、speed 3");
  });
});

describe("describeAction · 场景（scene_trigger）", () => {
  it("did/iid 落的是 scene_id，名字在 value_json 里", () => {
    expect(
      describeAction(
        {
          action_type: "scene_trigger",
          iid: "1234567",
          value_json: '{"scene_name":"回家"}',
        },
        // 场景不在设备 spec 里——这条路径不该依赖 spec。
        undefined,
        t,
      ),
    ).toBe("触发场景「回家」");
  });
});

describe("describeAction · 回落成原始键（返回 null）", () => {
  const cases: [string, Parameters<typeof describeAction>][] = [
    [
      "spec 整体取不到（设备不在 home / 规格缓存没热）",
      [
        { action_type: "set_property", iid: "prop.2.1", value_json: "true" },
        undefined,
        t,
      ],
    ],
    [
      "iid 不在 spec 里",
      [
        { action_type: "set_property", iid: "prop.8.8", value_json: "true" },
        spec,
        t,
      ],
    ],
    [
      "value_json 不是合法 JSON",
      [
        { action_type: "set_property", iid: "prop.2.1", value_json: "{oops" },
        spec,
        t,
      ],
    ],
    [
      "枚举值不在 value_list 里（固件 / 规格漂移）——不贴近似的名",
      [
        { action_type: "set_property", iid: "prop.3.2", value_json: "9" },
        spec,
        t,
      ],
    ],
    [
      "iid 为空",
      [{ action_type: "set_property", iid: null, value_json: "true" }, spec, t],
    ],
    [
      "没见过的 action_type",
      [
        { action_type: "unknown_kind", iid: "prop.2.1", value_json: "true" },
        spec,
        t,
      ],
    ],
    [
      "场景行没有 scene_name",
      [
        { action_type: "scene_trigger", iid: "1234567", value_json: "{}" },
        undefined,
        t,
      ],
    ],
  ];

  it.each(cases)("%s", (_name, args) => {
    expect(describeAction(...args)).toBeNull();
  });
});
