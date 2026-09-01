import { FormEvent, StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import "./index.css";

type Preview = {
  change_set_id: string;
  project_id: string;
  status: string;
  patch_hash: string;
  scope_hash: string;
  files: Array<{ relative_path: string; operation: string; before_size: number }>;
  diff: string;
  project_path?: string;
};

function App() {
  const [apiBase, setApiBase] = useState("http://127.0.0.1:18800");
  const [projectPath, setProjectPath] = useState("");
  const [relativePath, setRelativePath] = useState("src/example.py");
  const [operation, setOperation] = useState("update");
  const [expectedSha256, setExpectedSha256] = useState("");
  const [content, setContent] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [approvalToken, setApprovalToken] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const changeSetId = new URLSearchParams(window.location.search).get("change_set_id");
    if (!changeSetId || !/^[A-Za-z0-9_-]{1,128}$/.test(changeSetId)) return;
    let cancelled = false;
    setBusy(true);
    setMessage("正在加载 nanobot 生成的 Changeset…");
    void call(`/v1/change-sets/${encodeURIComponent(changeSetId)}`)
      .then((result) => {
        if (cancelled) return;
        const linkedPreview = result as Preview;
        setPreview(linkedPreview);
        setProjectPath(linkedPreview.project_path ?? "");
        const firstFile = linkedPreview.files?.[0]?.relative_path;
        if (firstFile) setRelativePath(firstFile);
        setMessage("Changeset 已加载，请检查 Diff 后再审批。");
      })
      .catch((error) => {
        if (!cancelled) {
          setMessage(`Changeset 加载失败：${error instanceof Error ? error.message : "unknown error"}`);
        }
      })
      .finally(() => {
        if (!cancelled) setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const call = async (path: string, init?: RequestInit) => {
    const response = await fetch(`${apiBase.replace(/\/$/, "")}${path}`, init);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error?.code ?? `HTTP ${response.status}`);
    return payload;
  };

  const handlePreview = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    setApprovalToken(null);
    try {
      const result = await call("/v1/change-sets/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_path: projectPath,
          patches: [{ relative_path: relativePath, operation, content: operation === "delete" ? null : content, expected_sha256: expectedSha256 || null }],
        }),
      });
      setPreview(result as Preview);
      setMessage("Preview 已生成，请检查文件、哈希和 Diff 后再批准。");
    } catch (error) {
      setMessage(`Preview 失败：${error instanceof Error ? error.message : "unknown error"}`);
    } finally {
      setBusy(false);
    }
  };

  const approveAndApply = async () => {
    if (!preview) return;
    setBusy(true);
    try {
      const approval = await call(`/v1/change-sets/${preview.change_set_id}/approve`, {
        method: "POST",
      });
      const result = await call(`/v1/change-sets/${preview.change_set_id}/apply`, {
        method: "POST",
        headers: {
          Authorization: `DevPilotGrant ${approval.approval_token}`,
          "Idempotency-Key": crypto.randomUUID(),
        },
      });
      setApprovalToken(null);
      setPreview({ ...preview, status: result.status });
      setMessage("Apply 完成。若要回滚，必须重新批准 rollback 能力。");
    } catch (error) {
      setMessage(`Apply 失败：${error instanceof Error ? error.message : "unknown error"}`);
    } finally {
      setBusy(false);
    }
  };

  const approveRollback = async () => {
    if (!preview) return;
    setBusy(true);
    try {
      const approval = await call(`/v1/change-sets/${preview.change_set_id}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ capability: "r1.rollback" }),
      });
      setApprovalToken(approval.approval_token as string);
      setMessage("Rollback 已获批，点击确认执行；令牌只保存在本页面内存。");
    } catch (error) {
      setMessage(`Rollback 审批失败：${error instanceof Error ? error.message : "unknown error"}`);
    } finally {
      setBusy(false);
    }
  };

  const verifyInSandbox = async () => {
    if (!preview) return;
    const profile = relativePath.toLowerCase().endsWith(".py")
      ? "python-syntax"
      : [".js", ".mjs", ".cjs"].some((suffix) => relativePath.toLowerCase().endsWith(suffix))
        ? "node-syntax"
        : null;
    if (!profile) {
      setMessage("当前 UI 仅支持 Python/Node 语法验证 Profile。请先选择对应代码文件。 ");
      return;
    }
    setBusy(true);
    try {
      const approval = await call(`/v1/change-sets/${preview.change_set_id}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ capability: "r2.verify" }),
      });
      const result = await call(`/v1/change-sets/${preview.change_set_id}/verify`, {
        method: "POST",
        headers: {
          Authorization: `DevPilotGrant ${approval.approval_token}`,
          "Idempotency-Key": crypto.randomUUID(),
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ profile }),
      });
      setPreview({ ...preview, status: result.status });
      setMessage(result.status === "verified" ? "Sandbox 验证通过。" : "Sandbox 验证失败，请查看输出后决定是否回滚。");
    } catch (error) {
      setMessage(`Sandbox 验证失败：${error instanceof Error ? error.message : "unknown error"}`);
    } finally {
      setBusy(false);
    }
  };

  const rollback = async () => {
    if (!preview || !approvalToken) return;
    setBusy(true);
    try {
      const result = await call(`/v1/change-sets/${preview.change_set_id}/rollback`, {
        method: "POST",
        headers: {
          Authorization: `DevPilotGrant ${approvalToken}`,
          "Idempotency-Key": crypto.randomUUID(),
        },
      });
      setApprovalToken(null);
      setPreview({ ...preview, status: result.status });
      setMessage("Rollback 完成。");
    } catch (error) {
      setMessage(`Rollback 失败：${error instanceof Error ? error.message : "unknown error"}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="shell">
      <header>
        <p className="eyebrow">DEVPILOT · P2B CONTROL PLANE</p>
        <h1>Review before code delivery</h1>
        <p className="subtle">nanobot 负责提出计划，用户负责审阅、批准和回滚。</p>
      </header>
      <section className="warning">Local approval + optional FC Sandbox verification · 不要用于真实生产仓库</section>
      <form className="card form" onSubmit={handlePreview}>
        <label>Control plane URL<input value={apiBase} onChange={(e) => setApiBase(e.target.value)} /></label>
        <label>Project path<input required value={projectPath} onChange={(e) => setProjectPath(e.target.value)} placeholder="D:\\projects\\demo" /></label>
        <div className="grid">
          <label>Relative file path<input required value={relativePath} onChange={(e) => setRelativePath(e.target.value)} /></label>
          <label>Operation<select value={operation} onChange={(e) => setOperation(e.target.value)}><option value="update">update</option><option value="create">create</option><option value="delete">delete</option></select></label>
        </div>
        <label>Expected SHA-256 (required for update/delete)<input value={expectedSha256} onChange={(e) => setExpectedSha256(e.target.value)} placeholder="64 lowercase hex characters" /></label>
        <label>New UTF-8 content<textarea value={content} onChange={(e) => setContent(e.target.value)} placeholder="Paste the complete replacement content" /></label>
        <button disabled={busy} type="submit">{busy ? "Working…" : "Generate preview"}</button>
      </form>
      {preview && <section className="card result">
        <div className="result-head"><div><p className="eyebrow">CHANGESET</p><h2>{preview.status}</h2></div><code>{preview.change_set_id}</code></div>
        <p>Patch hash: <code>{preview.patch_hash}</code></p>
        <p>Scope hash: <code>{preview.scope_hash}</code></p>
        <ul>{preview.files.map((file) => <li key={file.relative_path}><strong>{file.operation}</strong> {file.relative_path}</li>)}</ul>
        <pre>{preview.diff || "No textual diff"}</pre>
        <div className="actions">
          {preview.status === "planned" || preview.status === "approval_pending" ? <button disabled={busy} onClick={approveAndApply}>Approve & apply</button> : null}
          {preview.status === "applied" ? <button className="secondary" disabled={busy} onClick={verifyInSandbox}>Approve & verify in Sandbox</button> : null}
          {["applied", "verified", "verification_failed"].includes(preview.status) && !approvalToken ? <button className="secondary" disabled={busy} onClick={approveRollback}>Approve rollback</button> : null}
          {approvalToken ? <button className="danger" disabled={busy} onClick={rollback}>Confirm rollback</button> : null}
        </div>
      </section>}
      {message && <p className="message">{message}</p>}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
