import { createServer } from "node:http";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
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
  const path = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
  if (path === "/healthz") return json(response, { status: "ok" });
  if (path === "/api/v1/projects") return json(response, { projects: [] });
  if (path === "/api/v1/skills") return json(response, { skills: [] });
  if (path === "/api/v1/security/backends") return json(response, { backends: [] });
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
  const url = `http://127.0.0.1:${webPort}/`;
  const candidates = [
    process.env.DEVPILOT_BROWSER,
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  ].filter((candidate) => candidate && existsSync(candidate));
  if (candidates.length === 0) throw new Error("Edge or Chrome executable not found");
  // Chrome on this workstation cannot create a usable headless profile under
  // the locked-down Windows TEMP tree. Keep the profile ephemeral, but place
  // it under the project drive; callers on another machine can override this.
  const profileRoot = process.env.DEVPILOT_BROWSER_PROFILE_ROOT ??
    (process.platform === "win32" ? "E:\\codex-local" : "/tmp");
  const profile = await mkdtemp(join(profileRoot, "devpilot-browser-smoke-"));

  try {
    const result = spawnSync(candidates[0], [
      "--headless",
      "--disable-gpu",
      "--no-sandbox",
      "--disable-dev-shm-usage",
      "--disable-extensions",
      "--disable-background-networking",
      "--disable-component-update",
      "--no-default-browser-check",
      "--no-first-run",
      `--user-data-dir=${profile}`,
      "--virtual-time-budget=5000",
      "--dump-dom",
      url,
    ], { encoding: "utf8", timeout: 30000, windowsHide: true });
    if (result.error) throw result.error;
    if (result.status !== 0) throw new Error(`headless browser exited with ${result.status}: ${result.stderr}`);
    const dom = result.stdout;
    for (const marker of ["DevPilot Console", "P5 LOCAL CONSOLE", "API 在线"]) {
      if (!dom.includes(marker)) throw new Error(`browser fixture is missing marker: ${marker}`);
    }
    console.log(`browser smoke ok: fixture-api=${apiBase} browser=${candidates[0]}`);
  } finally {
    await rm(profile, { recursive: true, force: true });
  }
} finally {
  webServer.close();
  apiServer.close();
}
