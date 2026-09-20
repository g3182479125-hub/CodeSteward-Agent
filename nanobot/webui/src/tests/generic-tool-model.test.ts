import { describe, expect, it } from "vitest";

import { redactActivityText } from "@/components/thread/activity/activity-text";
import {
  describeGenericToolRun,
  parseGenericToolTrace,
  type GenericToolStatus,
} from "@/components/thread/activity/generic-tool-model";

function describeRun(line: string, status: GenericToolStatus = "done") {
  const trace = parseGenericToolTrace(line);
  expect(trace).not.toBeNull();
  return describeGenericToolRun([{ trace: trace!, status }]);
}

describe("generic tool activity semantics", () => {
  it.each([
    ['find_files({"glob":"*.tsx"})', "已找到文件", "*.tsx"],
    ['grep({"pattern":"dream_cursor"})', "已搜索文件", "“dream_cursor”"],
    ['list_dir({"path":"memory"})', "已列出文件", "memory"],
    ['read_file({"path":"docs/guide.md"})', "已读取文件", "docs/guide.md"],
    ['memory_search({"query":"launch date"})', "已搜索记忆", "“launch date”"],
    ['generate_image({"prompt":"private launch art"})', "已生成图片", ""],
    ['spawn({"label":"Research competitors","task":"private task"})', "已委派任务", "Research competitors"],
    ['message({"channel":"telegram","content":"private message"})', "已发送消息", "telegram"],
    ['my({"action":"check","key":"context_window_tokens"})', "已检查 Agent 设置", "context_window_tokens"],
    ['my({"action":"set","key":"model","value":"private-model"})', "已更新 Agent 设置", "model"],
    ['cron({"action":"add","name":"Daily digest","message":"private prompt"})', "已安排自动任务", "Daily digest"],
    ['cron({"action":"remove","name":"Daily digest"})', "已移除自动任务", "Daily digest"],
    ['create_goal({"objective":"private objective","ui_summary":"Benchmark memory"})', "已启动长任务", "Benchmark memory"],
    ['update_goal({"action":"complete","recap":"private recap"})', "已更新长任务", "complete"],
    ['write_stdin({"session_id":"session-1234567890-secret","chars":"private input"})', "已继续命令", "session…ecret"],
    ['list_exec_sessions({})', "已检查运行中的命令", ""],
    ['screenshot({"path":"artifacts/home.png"})', "已截取屏幕", ""],
    ['third_party_sync({"token":"secret","payload":"private payload"})', "工具执行完成 Third party sync", ""],
  ])("describes %s without exposing implementation syntax", (line, label, detail) => {
    const presentation = describeRun(line);
    expect(presentation.label).toBe(label);
    expect(presentation.detail).toBe(detail);
    expect(`${presentation.label} ${presentation.detail}`).not.toMatch(/[{}]|private|tool-results/);
  });

  it.each([
    ["running", "正在生成图片"],
    ["done", "已生成图片"],
    ["error", "生成图片失败"],
  ] as const)("uses human status copy for %s tools", (status, label) => {
    expect(describeRun('generate_image({"prompt":"private"})', status).label).toBe(label);
  });

  it("groups searches over collected sources without exposing absolute paths", () => {
    const first = parseGenericToolTrace(
      'grep({"pattern":"July","path":"/Users/test/.nanobot/tool-results/session/call_first.txt"})',
    )!;
    const second = parseGenericToolTrace(
      'grep({"pattern":"OpenAI","path":"/Users/test/.nanobot/tool-results/session/call_second.txt"})',
    )!;
    const presentation = describeGenericToolRun([
      { trace: first, status: "done" },
      { trace: second, status: "done" },
    ]);

    expect(presentation).toMatchObject({ label: "已检查来源", detail: "", aside: "2 个文件" });
    expect(JSON.stringify(presentation)).not.toContain("/Users/test");
  });

  it("shows the machine-readable execution reason in Chinese", () => {
    const trace = parseGenericToolTrace('read_file({"path":"secret.txt"})')!;

    expect(describeGenericToolRun([
      { trace, status: "error", code: "POLICY_DENIED", decision: "BLOCKED_BY_POLICY" },
    ]).aside).toBe("策略阻断");
    expect(describeGenericToolRun([
      { trace, status: "error", code: "TIMEOUT", decision: "PRECONDITION_FAILED" },
    ]).aside).toBe("等待超时");
    expect(describeGenericToolRun([
      { trace, status: "error", code: "OS_PERMISSION_DENIED", decision: "PRECONDITION_FAILED" },
    ]).aside).toBe("系统权限不足");
  });

  it("leaves specialized tools to their dedicated activity surfaces", () => {
    for (const line of [
      'web_search({"query":"nanobot"})',
      'web_fetch({"url":"https://example.com"})',
      'exec({"command":"date"})',
      'write_file({"path":"README.md"})',
      'edit_file({"path":"README.md"})',
      'apply_patch({"patch":"private"})',
      'run_cli_app({"name":"github"})',
      'mcp_browser_click({"text":"private"})',
    ]) {
      expect(parseGenericToolTrace(line)).toBeNull();
    }
  });

  it.each([
    ["Authorization: Bearer top-secret-token", "Authorization: <redacted>"],
    ["API_KEY=sk-proj-1234567890abcdef", "API_KEY=<redacted>"],
    ["--token xoxb-1234567890-secret", "--token <redacted>"],
    ["https://user:password@example.com/file?access_token=signed-secret", "https://<redacted>@example.com/file?access_token=<redacted>"],
    ["github ghp_1234567890abcdefghijkl", "github <redacted>"],
    ["aws AKIA1234567890ABCDEF", "aws <redacted>"],
    ["telegram 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd", "telegram <redacted>"],
  ])("redacts activity text before rendering: %s", (input, expected) => {
    expect(redactActivityText(input)).toBe(expected);
  });
});
