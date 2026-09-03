const consoleUrl = process.env.DEVPILOT_CONSOLE_URL ?? "http://127.0.0.1:18801";
const apiUrl = process.env.DEVPILOT_API_URL ?? "http://127.0.0.1:18800";

const consoleResponse = await fetch(`${consoleUrl}/`);
if (!consoleResponse.ok) throw new Error(`console returned ${consoleResponse.status}`);
const html = await consoleResponse.text();
if (!html.includes('id="root"') || !html.includes("src/main.tsx")) {
  throw new Error("console shell is missing the React root or entry module");
}

const healthResponse = await fetch(`${apiUrl}/healthz`);
if (!healthResponse.ok) throw new Error(`API returned ${healthResponse.status}`);
const health = await healthResponse.json();
if (health.status !== "ok") throw new Error("API health payload is not ok");

const skillsResponse = await fetch(`${apiUrl}/api/v1/skills`);
if (!skillsResponse.ok) throw new Error(`Skills endpoint returned ${skillsResponse.status}`);
const skills = await skillsResponse.json();
if (!Array.isArray(skills.skills)) throw new Error("Skills endpoint returned no catalog");

console.log(`smoke ok: console=${consoleUrl} api=${apiUrl} skills=${skills.skills.length}`);
