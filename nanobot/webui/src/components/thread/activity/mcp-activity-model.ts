import { localizeActivityText } from "./activity-copy";
import { safeActivityDetail } from "./activity-text";
import { formatCompactWebUrl, parseSafeActivityHttpUrl } from "./web-url";

export type McpActivityStatus = "running" | "done" | "error";

export interface McpActivityDescription {
  action: string;
  target?: string;
}

export function describeMcpActivity(
  toolName: string,
  args: unknown,
  status: McpActivityStatus,
): McpActivityDescription {
  const name = toolName.toLowerCase();

  if (matches(name, "navigate", "goto", "open_url", "visit")) {
    return describe(status, "正在打开", "已打开", "打开失败", value(args, ["url"]));
  }
  if (matches(name, "click", "tap")) {
    return describe(status, "正在点击", "已点击", "点击失败", elementTarget(args));
  }
  if (matches(name, "type", "fill", "enter_text", "insert_text")) {
    const target = value(args, ["element", "selector", "ref", "name"]);
    return describe(status, "正在输入文本", "已输入文本", "输入文本失败", target && `在 ${target}`);
  }
  if (matches(name, "press_key", "keypress")) {
    return describe(status, "正在按键", "已按键", "按键失败", value(args, ["key"]));
  }
  if (matches(name, "hover")) {
    return describe(status, "正在悬停于", "已悬停于", "悬停失败", elementTarget(args));
  }
  if (matches(name, "select", "select_option")) {
    return describe(status, "正在选择", "已选择", "选择失败", elementTarget(args));
  }
  if (matches(name, "snapshot", "inspect", "get_page_content", "page_content")) {
    return describe(status, "正在检查页面", "已检查页面", "检查页面失败");
  }
  if (matches(name, "screenshot", "capture_screenshot")) {
    return describe(status, "正在截取屏幕", "已截取屏幕", "截取屏幕失败");
  }
  if (matches(name, "wait", "wait_for")) {
    return describe(status, "正在等待页面", "已等待页面", "页面未就绪");
  }
  if (matches(name, "search", "web_search")) {
    return describe(status, "正在搜索", "已搜索", "搜索失败", value(args, ["query", "q"]));
  }

  if (status === "running") return { action: localizeActivityText(`正在执行工具 ${toolName}`) };
  if (status === "error") return { action: localizeActivityText(`工具执行失败 ${toolName}`) };
  return { action: localizeActivityText(`工具执行完成 ${toolName}`) };
}

function describe(
  status: McpActivityStatus,
  running: string,
  done: string,
  failed: string,
  target?: string,
): McpActivityDescription {
  return {
    action: localizeActivityText(status === "running" ? running : status === "error" ? failed : done),
    target: target ? localizeActivityText(compactUrl(target)) : undefined,
  };
}

function matches(name: string, ...actions: string[]): boolean {
  return actions.some((action) => name === action || name.endsWith(`_${action}`));
}

function elementTarget(args: unknown): string | undefined {
  return value(args, ["element", "selector", "ref", "name", "text"]);
}

function value(args: unknown, keys: string[]): string | undefined {
  if (!args || typeof args !== "object" || Array.isArray(args)) return undefined;
  const record = args as Record<string, unknown>;
  for (const key of keys) {
    const candidate = record[key];
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
    if (typeof candidate === "number" || typeof candidate === "boolean") return String(candidate);
  }
  return undefined;
}

function compactUrl(value: string): string {
  const url = parseSafeActivityHttpUrl(value);
  if (url) return formatCompactWebUrl(url);
  if (/^https?:\/\//i.test(value.trim())) return localizeActivityText("私有地址");
  return safeActivityDetail(value, 80);
}
