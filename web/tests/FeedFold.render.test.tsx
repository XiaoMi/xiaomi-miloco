/**
 * 折叠件的渲染契约:相位徽标、支行、成员表、强度分段控件。
 *
 * 这里用 renderToStaticMarkup(react-dom/server)把组件渲成**字符串**,在 node 环境
 * 下断言结构 —— 不需要 jsdom。守的是三类会静默腐烂的东西:
 *  1. 三件套 id 的对应关系(徽标 aria-controls → 成员表 id → 第一条动作 id,展开面的 id
 *     由摆放位置决定),它们各自算一套时症状是"点开以后焦点丢了",肉眼很难归因;
 *  2. 两档的语义差别(强档才写 aria-expanded / aria-controls,弱档恒为定位);
 *  3. 引用的 i18n key 是否真的存在 —— 缺 key 时 i18next 原样吐 key 字符串,界面显示
 *     "actions.badgeAria",而 zh/en 对齐测试查不出(两边都缺)。
 *
 * **断言一律定位到具体那枚控件**(byAttr / within),不用裸 toContain 扫全文:
 * 最初那版有三处 toContain 是被同页其它元素满足的,变异测试逐条改坏组件时有两条纹丝不动
 * (展开面 id 换成强档前缀、页脚 aria-controls 指错),等于没守。
 *
 * **边界(必读)**:静态渲染没有事件、没有 effect、没有布局。所以本文件守不住
 * 滚动落点、焦点交接、Esc 收起、徽标三态里的"已展开但滚出视口"、以及点击是否真的
 * 落在那枚按钮上 —— 那些只能靠点。全绿推不出交互正确。
 */

import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import "@/i18n"; // 初始化 i18next 单例(组件里的 useTranslation 依赖它)
import {
  BranchRow,
  FoldBadge,
  FoldChipPill,
  FoldMembers,
  StrengthToggle,
  firstMemberIdOf,
  hostRegionOf,
  membersIdOf,
} from "@/components/FeedFold";
import type { FoldBranch } from "@/lib/feedFold";
import type { ActivityEvent } from "@/lib/types";

const K = "ev-1::enter";

const HOST: ActivityEvent = {
  id: "ev-1",
  timestamp: 1_000_000,
  text: "厨房有人活动\n规则:开灯",
  device_ids: [],
  snapshot_count: 0,
};

function branch(extra: Partial<FoldBranch> = {}): FoldBranch {
  return {
    key: K,
    eventId: "ev-1",
    phase: "enter",
    ts: 1_000_100,
    actions: [
      {
        id: "act-1",
        timestamp: 1_000_050,
        did: "dev-1",
        device_name: "客厅灯",
        room: "客厅",
        action_type: "set_property",
        iid: "2.1",
        value_json: "true",
        success: 1,
        result_msg: null,
        error: null,
        trigger_event_id: "ev-1",
        phase: "enter",
      },
      {
        id: "act-2",
        timestamp: 1_000_100,
        did: "dev-2",
        device_name: "卧室空调",
        room: "卧室",
        action_type: "call_action",
        iid: null,
        value_json: null,
        success: 0,
        result_msg: null,
        error: "-704 限频",
        trigger_event_id: "ev-1",
        phase: "enter",
      },
    ],
    failed: true,
    chip: null,
    ...extra,
  };
}

const noop = () => {};

/* ── 断言工具 ───────────────────────────────────────────────
   把开标签解析成属性表,按属性定位到**那一枚**元素再断言。 */

function attrsOf(tag: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of tag.matchAll(/([a-zA-Z-]+)="([^"]*)"/g)) out[m[1]] = m[2];
  return out;
}

/** 页面上所有 `<tag>` 开标签的属性表,按出现次序。 */
function tagsIn(html: string, tag: string): Record<string, string>[] {
  return [...html.matchAll(new RegExp(`<${tag}\\b[^>]*>`, "g"))].map((m) => attrsOf(m[0]));
}

/** 唯一命中的那一枚(命中 0 个或多枚都算失败——多枚说明热区重了)。 */
function byAttr(html: string, tag: string, key: string, val: string): Record<string, string> {
  const hit = tagsIn(html, tag).filter((a) => a[key] === val);
  expect(hit, `<${tag} ${key}="${val}"> 应当恰好一枚`).toHaveLength(1);
  return hit[0];
}

/** 成员表那一段(从 `<ul id=...>` 到结尾):页脚出口在这段里,行头那枚开合器不在。 */
function membersOf(html: string): string {
  const at = html.indexOf(`<ul id="${membersIdOf(K)}"`);
  expect(at, "成员表没渲染出来").toBeGreaterThan(-1);
  return html.slice(at);
}

/** 每个用例都先过这一关:渲染出来的文本里不该有 key 本身。 */
function expectNoRawKeys(html: string) {
  expect(html).not.toMatch(/>\s*actions\.[a-zA-Z]/);
  expect(html).not.toMatch(/aria-label="[^"]*actions\./);
  expect(html).not.toMatch(/title="[^"]*actions\./);
}

/** id 是文档级唯一的。支行的行体与展开面曾经各写一个同值的 id(见 BranchRow 注释)。 */
function expectIdsUnique(html: string) {
  const ids = [...html.matchAll(/ id="([^"]*)"/g)].map((m) => m[1]);
  expect(ids.length).toBeGreaterThan(0);
  expect(ids, `重复 id: ${ids.filter((x, i) => ids.indexOf(x) !== i).join(", ")}`).toEqual([
    ...new Set(ids),
  ]);
}

function badgeHtml(opts: { strength: "weak" | "strong"; open: boolean }): string {
  return renderToStaticMarkup(
    <FoldBadge branch={branch()} strength={opts.strength} open={opts.open} onActivate={noop} />,
  );
}

function branchHtml(extra: {
  branch?: Partial<FoldBranch>;
  hostEvent?: ActivityEvent;
  hostRendered?: boolean;
  showEvents?: boolean;
  open: boolean;
}): string {
  return renderToStaticMarkup(
    <BranchRow
      branch={branch(extra.branch)}
      hostEvent={extra.hostEvent}
      hostRendered={extra.hostRendered ?? true}
      showEvents={extra.showEvents ?? true}
      open={extra.open}
      flash={false}
      specs={new Map()}
      onToggle={noop}
      onBack={noop}
      onLookupHost={noop}
    />,
  );
}

describe("FoldBadge — 一枚徽标一个热区,两档语义不同", () => {
  it("强档未展开:写 aria-expanded=false,但不写 aria-controls(成员表不在 DOM 里)", () => {
    const html = badgeHtml({ strength: "strong", open: false });
    const el = byAttr(html, "button", "data-jump", K);
    expect(el["aria-expanded"]).toBe("false");
    expect(el["aria-controls"]).toBeUndefined();
    // 整枚是一个按钮:里面的状态条与角标都是指示符,不能再有嵌套热区
    expect(tagsIn(html, "button")).toHaveLength(1);
    expectNoRawKeys(html);
  });

  it("强档已展开:aria-controls 指向成员表 id", () => {
    const el = byAttr(badgeHtml({ strength: "strong", open: true }), "button", "data-jump", K);
    expect(el["aria-expanded"]).toBe("true");
    expect(el["aria-controls"]).toBe(membersIdOf(K));
  });

  it("弱档不写 aria-expanded / aria-controls——它不宣称自己开合了什么", () => {
    const el = byAttr(badgeHtml({ strength: "weak", open: false }), "button", "data-jump", K);
    expect(el["aria-expanded"]).toBeUndefined();
    expect(el["aria-controls"]).toBeUndefined();
  });

  it("两档的读屏文本说的就是它实际会做的事", () => {
    const strong = badgeHtml({ strength: "strong", open: false });
    const weak = badgeHtml({ strength: "weak", open: false });
    // 读屏文本说的是「定位过去」,与它实际的动作为一致
    expect(byAttr(weak, "button", "data-jump", K)["aria-label"]).toContain("定位到该支");
    expect(byAttr(strong, "button", "data-jump", K)["aria-label"]).toContain("就地展开该支");
    expect(byAttr(badgeHtml({ strength: "strong", open: true }), "button", "data-jump", K)[
      "aria-label"
    ]).toContain("收起该支");
  });

  it("状态条一段一个动作,计数只进读屏文本(条本身对读屏隐藏)", () => {
    const html = badgeHtml({ strength: "strong", open: false });
    expect(html.match(/<i /g)).toHaveLength(2);
    expect(byAttr(html, "button", "data-jump", K)["aria-label"]).toContain("2 个，1 成功、1 失败");
  });
});

describe("FoldChipPill — 「未加载」可点,另外两种只是说明", () => {
  it("触发事件未加载 + 给了反查入口 → 按钮,且带一句它要干什么", () => {
    const html = renderToStaticMarkup(<FoldChipPill chip="hostMissing" onLookup={noop} />);
    const el = byAttr(html, "button", "title", "查一下这条触发事件在哪");
    expect(el["type"]).toBe("button");
    expect(html).toContain("触发事件未加载");
  });

  it("没给反查入口 → 退回不可点的文字(点了没反应的按钮比没有按钮更糟)", () => {
    const html = renderToStaticMarkup(<FoldChipPill chip="hostMissing" />);
    expect(tagsIn(html, "button")).toHaveLength(0);
    expect(html.startsWith("<span")).toBe(true);
  });

  it("无触发源与早于链路记录各说各的,不合并成一句", () => {
    const none = renderToStaticMarkup(<FoldChipPill chip="noTrigger" />);
    const pre = renderToStaticMarkup(<FoldChipPill chip="preLink" />);
    expect(none).toContain("无触发事件");
    expect(pre).toContain("早于链路记录");
    expect(pre).not.toContain("无触发事件");
  });
});

describe("BranchRow — 支行是弱档的主线,自己自足", () => {
  it("展开面挂在支行里:行体是滚动目标,展开面只写 data-region、不再写 id", () => {
    const html = branchHtml({ open: true });
    expectIdsUnique(html);
    // 行体的 id 就是这一档的滚动目标(ActivityFeed 里以支的键为参数);可编程聚焦是因为
    // 收起那一下展开面已从 DOM 里消失,滚动与焦点只能落回它。
    const li = byAttr(html, "li", "id", K);
    expect(li["tabindex"]).toBe("-1");
    const region = byAttr(html, "div", "data-region", K);
    expect(region["id"]).toBeUndefined();
    // 强档那套 id 前缀不该出现在这一档里(点开会滚向一个不存在的节点)
    expect(html).not.toContain(`id="${hostRegionOf(K)}"`);
    expectNoRawKeys(html);
  });

  it("宿主行画得出来时,标题上那枚 ↩ 才是热区;标题就是事件正文那行", () => {
    const withHost = branchHtml({ hostEvent: HOST, open: false });
    expect(byAttr(withHost, "button", "data-back", "ev-1")["aria-label"]).toBe("回到触发事件");
    expect(withHost).toContain("厨房有人活动");

    const withoutHost = branchHtml({ hostRendered: false, open: false });
    expect(tagsIn(withoutHost, "button").filter((a) => "data-back" in a)).toHaveLength(0);
  });

  it("事件流被用户关掉时不再解释宿主去哪了(那是他自己造成的)", () => {
    const html = branchHtml({
      branch: { eventId: null, chip: "hostMissing" },
      hostRendered: false,
      showEvents: false,
      open: false,
    });
    expect(html).not.toContain("触发事件未加载");
  });

  it("退出支带一枚延迟徽标,触发支没有", () => {
    const exit = branchHtml({
      branch: { key: "ev-1::exit", phase: "exit", ts: HOST.timestamp + 125_000 },
      hostEvent: HOST,
      open: false,
    });
    expect(exit).toContain("2 分 5 秒后");

    expect(branchHtml({ hostEvent: HOST, open: false })).not.toContain("后");
  });

  it("开合器两端各一枚:行头写 aria-controls,收起态不写", () => {
    const open = branchHtml({ hostEvent: HOST, open: true });
    const toggles = tagsIn(open, "button").filter((a) => a["data-toggle"] === K);
    expect(toggles, "展开态应当两端各一个出口").toHaveLength(2);
    for (const t of toggles) {
      expect(t["aria-expanded"]).toBe("true");
      expect(t["aria-controls"]).toBe(membersIdOf(K));
    }

    const closed = branchHtml({ hostEvent: HOST, open: false });
    const closedToggles = tagsIn(closed, "button").filter((a) => a["data-toggle"] === K);
    expect(closedToggles).toHaveLength(1);
    expect(closedToggles[0]["aria-expanded"]).toBe("false");
    expect(closedToggles[0]["aria-controls"]).toBeUndefined();
  });
});

describe("FoldMembers — 收起的出口在页脚,以及焦点落点", () => {
  it("第一条成员带 id 且可编程聚焦,后面几条不带", () => {
    const html = renderToStaticMarkup(
      <FoldMembers branch={branch()} specs={new Map()} onToggle={noop} />,
    );
    expectIdsUnique(html);
    expect(byAttr(html, "li", "id", firstMemberIdOf(K))["tabindex"]).toBe("-1");
    // 只有第一条给了 id(其余成员不该被当成跳转目标)
    expect(html.match(/id="[^"]*-m\d"/g)).toHaveLength(1);
  });

  it("页脚的收起出口带文字与数量——读到表尾时头部那个箭头已经滚出视野", () => {
    const html = renderToStaticMarkup(
      <FoldMembers branch={branch()} specs={new Map()} onToggle={noop} />,
    );
    const members = membersOf(html);
    // 页脚是成员表里最后一枚按钮
    const footer = tagsIn(members, "button").at(-1)!;
    expect(footer["data-toggle"]).toBe(K);
    expect(footer["aria-expanded"]).toBe("true");
    expect(footer["aria-controls"]).toBe(membersIdOf(K));
    expect(members).toContain("收起这 2 个动作");
    expectNoRawKeys(members);
  });
});

describe("StrengthToggle — 两档、roving tabindex", () => {
  it("单选组、两枚 radio,选中的那枚才在 Tab 序列里", () => {
    const html = renderToStaticMarkup(<StrengthToggle strength="strong" onChange={noop} />);
    expect(html).toContain('role="radiogroup"');
    const radios = tagsIn(html, "button").filter((a) => a["role"] === "radio");
    expect(radios).toHaveLength(2);
    expect(radios.map((r) => r["tabindex"])).toEqual(["-1", "0"]);
    expect(radios.map((r) => r["aria-checked"])).toEqual(["false", "true"]);
    expectNoRawKeys(html);
  });

  it("两档的名字与说明都在(i18n key 取不到时这里会红)", () => {
    const html = renderToStaticMarkup(<StrengthToggle strength="weak" onChange={noop} />);
    const radios = tagsIn(html, "button").filter((a) => a["role"] === "radio");
    expect(radios.map((r) => r["tabindex"])).toEqual(["0", "-1"]);
    expect(radios[0]["title"]).toContain("各行其时");
    expect(radios[1]["title"]).toContain("并入事件");
    expect(html).toContain("弱");
    expect(html).toContain("强");
  });
});

describe("展开三件套的 id 由摆放位置决定", () => {
  it("强档那片是派生 id,弱档那片就是支的键——两套不共用,也不与成员表重名", () => {
    expect(hostRegionOf("k")).toBe("x-k");
    expect(hostRegionOf("k")).not.toBe("k");
    // 成员表与首条成员各自一个命名空间,不与展开面重名
    expect(membersIdOf("k")).not.toBe(firstMemberIdOf("k"));
    expect(membersIdOf("k")).not.toBe(hostRegionOf("k"));
    expect(membersIdOf("k")).not.toBe("k");
    expect(firstMemberIdOf("k")).not.toBe("k");
    expect(firstMemberIdOf("k")).not.toBe(hostRegionOf("k"));
  });
});
