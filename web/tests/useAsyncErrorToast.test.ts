/**
 * 「转入错误才提醒」的闸门。
 *
 * 轮询驱动的拉取在后端宕机期间每个周期都会失败一次，而 Toast 的去重是为「同一操作
 * 连点」设的短窗口、挡不住这种重复；页面上又有多个带标签的轮询消费者，于是每次都弹
 * 就成了一条不会停的瀑布。同一个失败界面上本来就有内联错误条。
 */

import { describe, it, expect } from "vitest";
import { createErrorToastGate } from "@/hooks/useAsync";

describe("createErrorToastGate", () => {
  it("第一次失败放行，之后持续失败一律不放", () => {
    const g = createErrorToastGate();
    expect(g.fail()).toBe(true);
    for (let i = 0; i < 12; i++) expect(g.fail()).toBe(false); // 连续失败多轮
  });

  it("恢复成功后重新置位——下一次断开还要再提醒一次", () => {
    const g = createErrorToastGate();
    expect(g.fail()).toBe(true);
    expect(g.fail()).toBe(false);
    g.ok();
    expect(g.fail()).toBe(true);
  });

  it("连续成功不改变行为", () => {
    const g = createErrorToastGate();
    g.ok();
    g.ok();
    expect(g.fail()).toBe(true);
  });

  it("每个实例各自计数，互不影响", () => {
    const a = createErrorToastGate();
    const b = createErrorToastGate();
    expect(a.fail()).toBe(true);
    expect(b.fail()).toBe(true); // b 不该因为 a 已失败而被闭嘴
    expect(a.fail()).toBe(false);
  });
});
