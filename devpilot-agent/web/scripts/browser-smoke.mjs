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
  const path = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
  if (path === "/healthz") return json(response, { status: "ok" });
  if (path === "/api/v1/projects") return json(response, { projects: [] });
  if (path === "/api/v1/skills") return json(response, { skills: [] });
  if (path === "/api/v1/change-sets/fixture-change-set") return json(response, {
    change_set_id: "fixture-change-set", status: "verified", risk_summary: "fixture",
    patch_hash: "fixture-patch", diff: "--- a/fixture.txt\n+++ b/fixture.txt\n+ok\n",
  });
  if (path === "/api/v1/runs/fixture-run") return json(response, {
    run_id: "fixture-run", status: "completed", events: [{ event_id: 1, sequence: 1, event_type: "run.completed", created_at: "fixture", payload: {} }],
  });
  if (path === "/api/v1/runs/fixture-run/events") return json(response, { events: [] });
  if (path === "/api/v1/runs/fixture-run/artifacts") return json(response, { run_id: "fixture-run", artifacts: [] });
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
  const securityUrl = `http://127.0.0.1:${webPort}/?page=security`;
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
  await runBrowser(securityUrl, ["Execution Backends", "fixture-backend", "unavailable"]);
  for (const [page, marker] of [
    ["projects", "登记后由服务端校验允许根和仓库指纹"],
    ["changeset", "输入已有 ChangeSet ID，查看范围、风险和可审查 Diff"],
    ["runs", "SSE 是事件投影；连接失败时可刷新读取数据库快照"],
    ["artifacts", "只展示服务端拥有的证据元数据，不直连项目目录"],
    ["skills", "没有发现可展示的 Skill"],
  ]) {
    await runBrowser(`http://127.0.0.1:${webPort}/?page=${page}`, [marker]);
  }
  console.log(`browser smoke ok: fixture-api=${apiBase} browser=${candidates[0]}`);
} finally {
  webServer.close();
  apiServer.close();
}
