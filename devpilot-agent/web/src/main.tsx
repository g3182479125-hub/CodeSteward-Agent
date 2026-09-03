import type { FormEvent } from "react";
import { createRoot } from "react-dom/client";
import { useEffect, useRef, useState } from "react";
import "./index.css";

declare global {
  interface Window {
    __DEVPILOT_API_BASE__?: string;
    __DEVPILOT_HEALTH_URL__?: string;
  }
}

const DEFAULT_API = "http://127.0.0.1:18800/api/v1";

function loopbackUrl(value: string | undefined, fallback: string): string {
  try {
    const parsed = new URL(value ?? fallback);
    const loopback = parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost" || parsed.hostname === "[::1]";
    if (parsed.protocol !== "http:" || !loopback || parsed.username || parsed.password || parsed.search || parsed.hash) return fallback;
    return parsed.toString().replace(/\/$/, "");
  } catch {
    return fallback;
  }
}

const API = loopbackUrl(window.__DEVPILOT_API_BASE__ ?? import.meta.env.VITE_DEVPILOT_API_BASE, DEFAULT_API);
const HEALTH_URL = loopbackUrl(window.__DEVPILOT_HEALTH_URL__, `${API.replace(/\/api\/v1\/?$/, "")}/healthz`);

type Page = "dashboard" | "projects" | "changeset" | "runs" | "artifacts" | "skills" | "security";
type Json = Record<string, unknown>;

function initialPage(): Page {
  const candidate = new URLSearchParams(window.location.search).get("page");
  return ["dashboard", "projects", "changeset", "runs", "artifacts", "skills", "security"].includes(candidate ?? "")
    ? candidate as Page
    : "dashboard";
}

interface Project {
  project_id: string;
  project_name: string;
  trust_state: string;
  repo_fingerprint?: string | null;
  created_at?: string;
}

interface RunEvent {
  event_id: number;
  event_type?: string;
  type?: string;
  sequence: number;
  created_at: string;
  payload?: unknown;
  redacted_payload?: unknown;
}

function text(value: unknown, fallback = "—"): string {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

async function api<T extends Json>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const payload = (await response.json().catch(() => ({}))) as Json;
  if (!response.ok) {
    const error = payload.error as Json | undefined;
    throw new Error(text(error?.message, `请求失败 (${response.status})`));
  }
  return payload as T;
}

function Badge({ children, tone = "neutral" }: { children: string; tone?: "good" | "warn" | "bad" | "neutral" }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

function Empty({ children }: { children: string }) {
  return <div className="empty">{children}</div>;
}

function App() {
  const [page, setPage] = useState<Page>(initialPage);
  const [projects, setProjects] = useState<Project[]>([]);
  const [changeSetId, setChangeSetId] = useState("");
  const [changeSet, setChangeSet] = useState<Json | null>(null);
  const [runId, setRunId] = useState("");
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [artifacts, setArtifacts] = useState<Json[]>([]);
  const [skills, setSkills] = useState<Json[]>([]);
  const [backends, setBackends] = useState<Json[]>([]);
  const [projectPath, setProjectPath] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [health, setHealth] = useState("checking");
  const eventSource = useRef<EventSource | null>(null);
  const pollTimer = useRef<number | null>(null);

  const loadProjects = async () => {
    const result = await api<{ projects: Project[] }>("/projects");
    setProjects(result.projects);
  };

  const loadChangeSet = async (event: FormEvent) => {
    event.preventDefault();
    if (!changeSetId.trim()) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<Json>(`/change-sets/${encodeURIComponent(changeSetId.trim())}`);
      setChangeSet(result);
      const linkedRun = text(result.run_id, "");
      if (linkedRun) setRunId(linkedRun);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取 ChangeSet");
    } finally {
      setBusy(false);
    }
  };

  const loadRun = async (event: FormEvent) => {
    event.preventDefault();
    if (!runId.trim()) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<{ events?: RunEvent[] }>(`/runs/${encodeURIComponent(runId.trim())}`);
      setEvents(result.events ?? []);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取 Run");
    } finally {
      setBusy(false);
    }
  };

  const refreshRun = async () => {
    if (!runId.trim()) return;
    const result = await api<{ events?: RunEvent[] }>(`/runs/${encodeURIComponent(runId.trim())}`);
    setEvents(result.events ?? []);
  };

  const openEventStream = () => {
    if (!runId.trim()) return;
    setError("");
    eventSource.current?.close();
    if (pollTimer.current !== null) window.clearInterval(pollTimer.current);
    const source = new EventSource(`${API}/runs/${encodeURIComponent(runId.trim())}/events`);
    eventSource.current = source;
    const handleEvent = (message: MessageEvent<string>) => {
      try {
        const incoming = JSON.parse(message.data) as RunEvent;
        setEvents((current) => current.some((item) => item.event_id === incoming.event_id) ? current : [...current, incoming]);
      } catch {
        setError("收到无法解析的事件");
      }
    };
    for (const eventType of [
      "run.started", "run.completed", "run.failed", "run.updated",
      "plan.created", "plan.updated", "changeset.previewed", "changeset.approval_requested",
      "changeset.approval_issued", "changeset.approvals_revoked", "changeset.applied",
      "changeset.apply_failed", "changeset.reverted", "changeset.verification_completed",
      "changeset.verification_failed", "execution.capability_denied", "project.trust_denied",
      "delivery.accepted", "delivery.stopped", "tool.completed", "evidence.read.completed",
      "evidence.search.completed", "evidence.inspect.completed", "feature_delivery.proposed",
      "feature_delivery.failed", "bug_fix.proposed", "bug_fix.failed", "bug_fix.completed",
      "test_repair.proposed", "test_repair.failed", "test_repair.completed",
      "artifact.persistence_failed", "bug_check.failed", "test_check.failed",
    ]) {
      source.addEventListener(eventType, handleEvent as EventListener);
    }
    source.onerror = () => {
      source.close();
      eventSource.current = null;
      setError("SSE 暂时不可用，已自动切换为数据库轮询。");
      pollTimer.current = window.setInterval(() => {
        void refreshRun().catch(() => setError("SSE 和轮询均暂时不可用。"));
      }, 3000);
    };
  };

  useEffect(() => () => {
    eventSource.current?.close();
    if (pollTimer.current !== null) window.clearInterval(pollTimer.current);
  }, []);

  const loadArtifacts = async (event: FormEvent) => {
    event.preventDefault();
    if (!runId.trim()) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<{ artifacts?: Json[] }>(`/runs/${encodeURIComponent(runId.trim())}/artifacts`);
      setArtifacts(result.artifacts ?? []);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取 Artifact");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    void fetch(HEALTH_URL)
      .then((response) => setHealth(response.ok ? "online" : "offline"))
      .catch(() => setHealth("offline"));
  }, []);

  useEffect(() => {
    if (page === "projects") void loadProjects().catch((cause) => setError(cause instanceof Error ? cause.message : "无法读取项目"));
    if (page === "skills") void api<{ skills: Json[] }>("/skills").then((result) => setSkills(result.skills)).catch((cause) => setError(cause instanceof Error ? cause.message : "无法读取 Skills"));
    if (page === "security") void api<{ backends: Json[] }>("/security/backends").then((result) => setBackends(result.backends)).catch((cause) => setError(cause instanceof Error ? cause.message : "无法读取后端"));
    const query = new URLSearchParams(window.location.search);
    const queryChangeSetId = query.get("change_set_id");
    if (page === "changeset" && queryChangeSetId) {
      setChangeSetId(queryChangeSetId);
      void api<Json>(`/change-sets/${encodeURIComponent(queryChangeSetId)}`).then((result) => setChangeSet(result)).catch((cause) => setError(cause instanceof Error ? cause.message : "无法读取 ChangeSet"));
    }
    const queryRunId = query.get("run_id");
    if (page === "runs" && queryRunId) {
      setRunId(queryRunId);
      void api<{ events?: RunEvent[] }>(`/runs/${encodeURIComponent(queryRunId)}`).then((result) => setEvents(result.events ?? [])).catch((cause) => setError(cause instanceof Error ? cause.message : "无法读取 Run"));
    }
    if (page === "artifacts" && queryRunId) {
      setRunId(queryRunId);
      void api<{ artifacts?: Json[] }>(`/runs/${encodeURIComponent(queryRunId)}/artifacts`).then((result) => setArtifacts(result.artifacts ?? [])).catch((cause) => setError(cause instanceof Error ? cause.message : "无法读取 Artifact"));
    }
  }, [page]);

  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    const queryRunId = query.get("run_id");
    if (page !== "runs" || query.get("sse") !== "1" || !queryRunId) return;
    const source = new EventSource(`${API}/runs/${encodeURIComponent(queryRunId)}/events`);
    const handleEvent = (message: MessageEvent<string>) => {
      try {
        const incoming = JSON.parse(message.data) as RunEvent;
        setEvents((current) => current.some((item) => item.event_id === incoming.event_id) ? current : [...current, incoming]);
      } catch {
        setError("收到无法解析的事件");
      }
    };
    source.addEventListener("run.completed", handleEvent as EventListener);
    return () => source.close();
  }, [page]);

  const registerProject = async (event: FormEvent) => {
    event.preventDefault();
    if (!projectPath.trim()) return;
    setBusy(true);
    setError("");
    try {
      await api<Json>("/projects", { method: "POST", body: JSON.stringify({ project_path: projectPath.trim() }) });
      setProjectPath("");
      await loadProjects();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "项目登记失败");
    } finally {
      setBusy(false);
    }
  };

  const nav: Array<[Page, string, string]> = [
    ["dashboard", "总览", "Dashboard"],
    ["projects", "项目", "Projects"],
    ["changeset", "变更审查", "ChangeSet / Diff"],
    ["runs", "运行时间线", "Run Timeline"],
    ["artifacts", "证据产物", "Artifacts"],
    ["skills", "Skills", "Skills"],
    ["security", "安全后端", "Security / Backends"],
  ];

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">DP</span><div><strong>DevPilot</strong><small>Local Console</small></div></div>
        <nav aria-label="主导航">
          {nav.map(([key, label, hint]) => <button className={page === key ? "nav-item active" : "nav-item"} key={key} onClick={() => { setPage(key); setError(""); }}><span>{label}</span><small>{hint}</small></button>)}
        </nav>
        <div className="sidebar-note">nanobot 聊天入口保持不变<br />危险动作仍由显式确认控制</div>
      </aside>
      <main className="main">
        <header className="topbar"><div><p className="eyebrow">LOCAL DEVELOPMENT CONTROL PLANE</p><h1>{nav.find(([key]) => key === page)?.[2]}</h1></div><div className="health"><span className={`dot ${health}`} /> API {health}</div></header>
        {error && <div className="alert" role="alert">{error}</div>}
        {page === "dashboard" && <Dashboard projects={projects} health={health} onProjects={() => { setPage("projects"); void loadProjects(); }} />}
        {page === "projects" && <section className="content"><div className="section-head"><div><h2>Projects</h2><p>登记后由服务端校验允许根和仓库指纹；页面不展示绝对路径。</p></div><button className="ghost" onClick={() => void loadProjects()}>刷新</button></div><form className="inline-form" onSubmit={registerProject}><label>本地项目路径<input value={projectPath} onChange={(event) => setProjectPath(event.target.value)} placeholder="E:\\projects\\my-agent" /></label><button className="primary" disabled={busy}>登记项目</button></form><div className="card-list">{projects.length === 0 ? <Empty>还没有登记项目。</Empty> : projects.map((project) => <article className="list-row" key={project.project_id}><div><strong>{project.project_id}</strong><p>{project.project_name}</p></div><Badge tone={project.trust_state === "trusted" ? "good" : "warn"}>{project.trust_state}</Badge></article>)}</div></section>}
        {page === "changeset" && <section className="content"><div className="section-head"><div><h2>ChangeSet / Diff</h2><p>输入已有 ChangeSet ID，查看范围、风险和可审查 Diff。</p></div></div><form className="inline-form" onSubmit={loadChangeSet}><label>ChangeSet ID<input value={changeSetId} onChange={(event) => setChangeSetId(event.target.value)} placeholder="粘贴 ChangeSet ID" /></label><button className="primary" disabled={busy}>读取</button></form>{changeSet ? <ChangeSetView data={changeSet} onDecision={async (decision, displayedChangeSetId) => { const confirmation = decision === "accept" ? "接受" : "停止"; const label = decision === "accept" ? "接受交付" : "停止交付"; if (!window.confirm(`请再次确认：${label}？`)) return; setBusy(true); setError(""); try { const key = `console-${crypto.randomUUID()}`; const result = await api<Json>(`/change-sets/${encodeURIComponent(displayedChangeSetId)}/decision`, { method: "POST", headers: { "Idempotency-Key": key }, body: JSON.stringify({ decision, user_confirmation: confirmation }) }); setChangeSet((current) => ({ ...(current ?? {}), ...result })); } catch (cause) { setError(cause instanceof Error ? cause.message : "交付决策失败"); } finally { setBusy(false); } }} /> : <Empty>尚未选择 ChangeSet。</Empty>}</section>}
        {page === "runs" && <section className="content"><div className="section-head"><div><h2>Run Timeline</h2><p>SSE 是事件投影；连接失败时可刷新读取数据库快照。</p></div></div><form className="inline-form" onSubmit={loadRun}><label>Run ID<input value={runId} onChange={(event) => setRunId(event.target.value)} placeholder="粘贴 Run ID" /></label><button className="primary" disabled={busy}>读取快照</button><button type="button" className="ghost" onClick={openEventStream}>连接 SSE</button></form><Timeline events={events} /></section>}
        {page === "artifacts" && <section className="content"><div className="section-head"><div><h2>Artifacts</h2><p>只展示服务端拥有的证据元数据，不直连项目目录。</p></div></div><form className="inline-form" onSubmit={loadArtifacts}><label>Run ID<input value={runId} onChange={(event) => setRunId(event.target.value)} placeholder="粘贴 Run ID" /></label><button className="primary" disabled={busy}>读取产物</button></form><div className="card-list">{artifacts.length === 0 ? <Empty>尚未读取 Artifact。</Empty> : artifacts.map((artifact) => <article className="list-row" key={text(artifact.id)}><div><strong>{text(artifact.kind)}</strong><p>{text(artifact.relative_path)}</p></div><code>{text(artifact.sha256).slice(0, 12)}…</code></article>)}</div></section>}
        {page === "skills" && <Catalog title="Skills Catalog" items={skills} empty="没有发现可展示的 Skill。" />}
        {page === "security" && <Catalog title="Execution Backends" items={backends} empty="没有发现后端记录。" />}
      </main>
    </div>
  );
}

function Dashboard({ projects, health, onProjects }: { projects: Project[]; health: string; onProjects: () => void }) {
  return <section className="content"><div className="hero"><div><p className="eyebrow">P5 LOCAL CONSOLE</p><h2>把计划、变更、验证和证据放在同一个可审查界面。</h2><p>这是 DevPilot 的可选控制台；nanobot 仍然负责聊天和自然语言入口。</p></div><Badge tone={health === "online" ? "good" : "warn"}>{health === "online" ? "API 在线" : "API 未确认"}</Badge></div><div className="stats"><div><span>Projects</span><strong>{projects.length}</strong></div><div><span>审批原则</span><strong>显式确认</strong></div><div><span>执行能力</span><strong>验证专用</strong></div></div><div className="card"><div className="section-head"><div><h3>开始使用</h3><p>先登记项目，再从 nanobot 获取 ChangeSet ID 或 Run ID。</p></div><button className="primary" onClick={onProjects}>管理项目</button></div><div className="steps"><span>01 项目登记</span><span>02 审查 Diff</span><span>03 查看 Run / SSE</span><span>04 下载证据</span></div></div></section>;
}

function ChangeSetView({ data, onDecision }: { data: Json; onDecision: (decision: "accept" | "stop", changeSetId: string) => Promise<void> }) {
  const status = text(data.status);
  const diff = text(data.diff, "暂无 Diff");
  const changeSetId = text(data.change_set_id, "");
  return <div className="stack"><div className="summary-grid"><div><span>状态</span><strong>{status}</strong></div><div><span>风险</span><strong>{text(data.risk_summary)}</strong></div><div><span>Patch hash</span><code>{text(data.patch_hash).slice(0, 16)}…</code></div></div><details className="diff" open><summary>展开 / 收起 Diff <span>{diff.split("\n").length} 行</span></summary><pre>{diff}</pre></details><div className="notice">Apply、Verify、Rollback 仍由 nanobot 的既有确认链路发起；此页面不会接收或保存 capability token。</div><div className="decision-card"><strong>最终交付决策</strong><p>仅在验证完成后使用。点击后还会弹出一次浏览器确认，不会直接修改项目文件。</p><div className="button-row"><button className="primary" type="button" disabled={!changeSetId} onClick={() => void onDecision("accept", changeSetId)}>接受交付</button><button className="danger" type="button" disabled={!changeSetId} onClick={() => void onDecision("stop", changeSetId)}>停止交付</button></div></div></div>;
}

function Timeline({ events }: { events: RunEvent[] }) {
  if (events.length === 0) return <Empty>尚未读取 Run 事件。</Empty>;
  return <div className="timeline">{events.map((event) => <article key={event.event_id}><div className="timeline-marker">{event.sequence}</div><div><strong>{text(event.event_type ?? event.type)}</strong><time>{event.created_at}</time><pre>{JSON.stringify(event.payload ?? event.redacted_payload ?? {}, null, 2)}</pre></div></article>)}</div>;
}

function Catalog({ title, items, empty }: { title: string; items: Json[]; empty: string }) {
  return <section className="content"><div className="section-head"><div><h2>{title}</h2><p>状态由后端能力摘要提供，不把未验证能力伪装成可用。</p></div></div><div className="card-list">{items.length === 0 ? <Empty>{empty}</Empty> : items.map((item, index) => <article className="list-row" key={text(item.id, text(item.name, String(index)))}><div><strong>{text(item.id, text(item.name))}</strong><p>{text(item.description, text(item.capability, "服务端能力摘要"))}{item.verified === false ? " · 尚未验证" : ""}</p></div><Badge tone={item.status === "active" && item.verified !== false ? "good" : "warn"}>{text(item.status)}</Badge></article>)}</div></section>;
}

createRoot(document.getElementById("root")!).render(<App />);
