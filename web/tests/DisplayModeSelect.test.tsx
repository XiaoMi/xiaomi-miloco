import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import "@/i18n";
import { DisplayModeSelect, actionDisplayState } from "@/components/FeedFold";

describe("activity display mode", () => {
  it.each(["individual", "grouped", "inline"] as const)("uses one stable select for %s", (mode) => {
    const html = renderToStaticMarkup(<DisplayModeSelect mode={mode} onChange={() => {}} />);
    expect(html.match(/<select\b/g)).toHaveLength(1);
    expect(html.match(/<option\b/g)).toHaveLength(3);
    expect(html).not.toContain("<button");
    expect(html).toContain("动作显示方式");
    expect(html).toContain("逐条显示");
    expect(html).toContain("动作分组");
    expect(html).toContain("并入事件");
    expect(html).toContain(`value="${mode}" selected=""`);
    expect(html).toContain('w-36');
    expect(html).not.toContain("actions.display");
  });

  it("uses a native disabled select without losing the saved mode", () => {
    const html = renderToStaticMarkup(<DisplayModeSelect mode="inline" disabled onChange={() => {}} />);
    expect(html).toMatch(/<select[^>]*disabled=""/);
    expect(html).toContain('value="inline" selected=""');
    expect(html).toContain('aria-describedby="activity-display-hint"');
  });

  it.each(["individual", "grouped", "inline"] as const)("explains an empty action list in %s mode", (mode) => {
    expect(actionDisplayState(mode, true, 0, false)).toEqual({ disabled: true, hint: "actions.displayNoActions" });
  });

  it("explains hidden actions instead of claiming no records exist", () => {
    expect(actionDisplayState("inline", false, 0, false)).toEqual({ disabled: true, hint: "actions.displayActionsHidden" });
  });

  it("restores the control when actions become visible", () => {
    expect(actionDisplayState("grouped", true, 2, true)).toEqual({ disabled: false, hint: "actions.displayHint_grouped" });
    expect(actionDisplayState("inline", true, 2, false)).toEqual({ disabled: false, hint: "actions.displayNoGroups" });
    expect(actionDisplayState("individual", true, 2, false)).toEqual({ disabled: false, hint: "actions.displayHint_individual" });
  });
});
