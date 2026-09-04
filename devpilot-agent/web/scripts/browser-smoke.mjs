import { createServer } from "node:http";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { spawn } from "node:child_process";
import { dirname, extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dist = join(root, "dist");
if (!existsSync(join(dist, "index.html"))) {
  throw new Error("web/dist is missing; run pnpm build before browser-smoke");
}

const json = (response, payload, status = 200) => {
  response.writeHead(status, {
    "access-control-allow-origin": "*",
    "content-type": "application/json",
  });
  response.end(JSON.stringify(payload));
};

const requestedPaths = new Set();
let runSnapshotRequests = 0;
let failedRunSnapshotRequests = 0;
const apiServer = createServer((request, response) => {
  if (request.method === "OPTIONS") {
    response.writeHead(204, {
      "access-control-allow-headers": "Content-Type",
      "access-control-allow-methods": "GET, OPTIONS",
      "access-control-allow-origin": "*",
    });
    response.end();
    return;
  }
  const requestUrl = new URL(request.url ?? "/", "http://127.0.0.1");
  const path = requestUrl.pathname;
  requestedPaths.add(path);
  if (path === "/healthz") return json(response, { status: "ok" });
  if (path === "/api/v1/projects") return json(response, { projects: [{ project_id: "fixture-project", project_name: "fixture-project", trust_state: "trusted" }] });
  if (path === "/api/v1/skills") return json(response, { skills: [{ name: "fixture-skill", capability: "fixture" }] });
  if (path === "/api/v1/change-sets/fixture-change-set") return json(response, {
    change_set_id: "fixture-change-set", status: "verified", risk_summary: "fixture",
    patch_hash: "fixture-patch", diff: "--- a/fixture.txt\n+++ b/fixture.txt\n+ok\n",
  });
  if (path === "/api/v1/change-sets/fixture-change-set/delivery-report") return json(response, {
    change_set_id: "fixture-change-set", status: "verified", available_decisions: ["accept", "stop", "rollback"],
    decision: { type: null }, rollback: { capability: "r1.rollback", requires_approval_token: true },
  });
  if (path === "/api/v1/runs/fixture-run" || path === "/api/v1/runs/fixture-failed-sse") {
    runSnapshotRequests += 1;
    if (path.endsWith("fixture-failed-sse")) failedRunSnapshotRequests += 1;
    return json(response, {
    run_id: path.endsWith("fixture-failed-sse") ? "fixture-failed-sse" : "fixture-run", status: "completed", events: [{ event_id: 1, sequence: 1, event_type: "run.completed", created_at: "fixture", payload: {} }],
    });
  }
  if (path === "/api/v1/runs/fixture-run/events" || path === "/api/v1/runs/fixture-failed-sse/events") {
    response.writeHead(200, {
      "access-control-allow-origin": "*",
      "cache-control": "no-cache",
      "content-type": "text/event-stream",
    });
    if (path.endsWith("fixture-failed-sse/events")) {
      response.end();
      return;
    }
    response.write('id: 2\nevent: run.started\ndata: {"event_id":2,"sequence":2,"event_type":"run.started","created_at":"fixture","payload":{}}\n\nid: 3\nevent: run.completed\ndata: {"event_id":3,"sequence":3,"event_type":"run.completed","created_at":"fixture","payload":{}}\n\n');
    setTimeout(() => response.end(), 5000);
    return;
  }
  if (path === "/api/v1/runs/fixture-run/artifacts") return json(response, { run_id: "fixture-run", artifacts: [{ id: "fixture-artifact", kind: "fixture", relative_path: "fixture.md", sha256: "fixture-hash" }] });
  if (path === "/api/v1/security/backends") return json(response, {
    backends: [{ name: "fixture-backend", status: "unavailable", verified: false, capability: "verification-only" }],
  });
  return json(response, { error: { code: "NOT_FOUND", message: "fixture route not found" } }, 404);
});

const listen = (server) => new Promise((resolvePromise, reject) => {
  server.once("error", reject);
  server.listen(0, "127.0.0.1", () => {
    server.removeListener("error", reject);
    resolvePromise(server.address().port);
  });
});

const apiPort = await listen(apiServer);
const apiBase = `http://127.0.0.1:${apiPort}/api/v1`;
const webServer = createServer(async (request, response) => {
  const requestPath = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
  const relative = requestPath === "/" ? "index.html" : requestPath.slice(1);
  const filePath = resolve(dist, relative);
  if (!filePath.startsWith(`${dist}${process.platform === "win32" ? "\\" : "/"}`)) {
    response.writeHead(403).end();
    return;
  }
  try {
    let body = await readFile(filePath);
    if (relative === "index.html") {
      const bootstrap = `<script>window.__DEVPILOT_API_BASE__=${JSON.stringify(apiBase)};window.__DEVPILOT_HEALTH_URL__=${JSON.stringify(`http://127.0.0.1:${apiPort}/healthz`)};</script>`;
      body = Buffer.from(body.toString().replace("<head>", `<head>${bootstrap}`));
    }
    const contentTypes = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
    response.writeHead(200, { "content-type": contentTypes[extname(filePath)] ?? "application/octet-stream" });
    response.end(body);
  } catch {
    response.writeHead(404).end();
  }
});

try {
  const webPort = await listen(webServer);
  const dashboardUrl = `http://127.0.0.1:${webPort}/`;
  const candidates = [
    process.env.DEVPILOT_BROWSER,
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  ].filter((candidate) => candidate && existsSync(candidate));
  if (candidates.length === 0) throw new Error("Edge or Chrome executable not found");
  const profileRoot = process.env.DEVPILOT_BROWSER_PROFILE_ROOT ?? tmpdir();
  const runBrowser = async (url, markers) => {
    const profile = await mkdtemp(join(profileRoot, "devpilot-browser-smoke-"));
    return new Promise((resolvePromise, reject) => {
      const child = spawn(candidates[0], [
        "--headless",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-component-update",
        "--no-default-browser-check",
        "--no-first-run",
        `--user-data-dir=${profile}`,
        "--virtual-time-budget=10000",
        "--dump-dom",
        url,
      ], { encoding: "utf8", windowsHide: true });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (chunk) => { stdout += chunk; });
      child.stderr.on("data", (chunk) => { stderr += chunk; });
      const timeout = setTimeout(() => {
        child.kill();
        reject(new Error(`headless browser timed out for ${url}`));
      }, 30000);
      child.on("error", reject);
      child.on("close", async (code) => {
        clearTimeout(timeout);
        await rm(profile, { recursive: true, force: true });
        if (code !== 0) {
          reject(new Error(`headless browser exited with ${code}: ${stderr}`));
          return;
        }
        for (const marker of markers) {
          if (!stdout.includes(marker)) {
            reject(new Error(`browser fixture is missing marker: ${marker}; DOM tail: ${stdout.slice(-1000)}`));
            return;
          }
        }
        resolvePromise();
      });
    });
  };
  await runBrowser(dashboardUrl, ["把计划、变更、验证和证据放在同一个可审查界面。", "P5 LOCAL CONSOLE", "API 在线"]);
  for (const [query, markers] of [
    ["page=projects", ["fixture-project", "登记项目"]],
    ["page=changeset&change_set_id=fixture-change-set", ["fixture.txt", "展开 / 收起 Diff", "审批状态", "接受交付", "停止交付", "申请回滚"]],
    ["page=runs&run_id=fixture-run&sse=1", ["run.started", "run.completed", "读取快照", "连接 SSE"]],
    ["page=runs&run_id=fixture-failed-sse&sse=1", ["SSE 暂时不可用", "读取快照"]],
    ["page=artifacts&run_id=fixture-run", ["fixture.md", "读取产物"]],
    ["page=skills", ["fixture-skill", "Skills Catalog"]],
    ["page=security", ["Execution Backends", "fixture-backend", "unavailable"]],
  ]) {
    await runBrowser(`http://127.0.0.1:${webPort}/?${query}`, markers);
  }
  if (runSnapshotRequests < 2) throw new Error("browser fixture did not exercise polling fallback");
  if (failedRunSnapshotRequests < 2) throw new Error("browser fixture did not poll after SSE failure");
  for (const path of [
    "/api/v1/projects", "/api/v1/skills", "/api/v1/security/backends",
    "/api/v1/change-sets/fixture-change-set", "/api/v1/change-sets/fixture-change-set/delivery-report", "/api/v1/runs/fixture-run",
    "/api/v1/runs/fixture-run/artifacts", "/api/v1/runs/fixture-run/events",
    "/api/v1/runs/fixture-failed-sse", "/api/v1/runs/fixture-failed-sse/events",
  ]) {
    if (!requestedPaths.has(path)) throw new Error(`browser fixture did not receive request: ${path}`);
  }
  console.log(`browser smoke ok: fixture-api=${apiBase} browser=${candidates[0]}`);
} finally {
  webServer.close();
  apiServer.close();
}
