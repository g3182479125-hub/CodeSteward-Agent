import { describe, expect, it } from "vitest";

import i18n from "@/i18n";
import { localizeActivityText } from "@/components/thread/activity/activity-copy";

describe("简体中文活动文案", () => {
  it("在简体中文界面保留中文工作摘要", async () => {
    await i18n.changeLanguage("zh-CN");

    expect(localizeActivityText("已搜索网页")).toBe("已搜索网页");
    expect(localizeActivityText("工具执行完成 Third party sync")).toBe(
      "工具执行完成 Third party sync",
    );
    expect(localizeActivityText("2 个文件")).toBe("2 个文件");
    expect(localizeActivityText("npm test · 脚本，共 3 行")).toBe(
      "npm test · 脚本，共 3 行",
    );
  });
});
