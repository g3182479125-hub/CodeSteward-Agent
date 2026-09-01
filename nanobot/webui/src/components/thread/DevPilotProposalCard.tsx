import { ExternalLink, ShieldCheck } from "lucide-react";

import type { ToolProgressEvent, UIMessage } from "@/lib/types";
import { cn } from "@/lib/utils";

const DEFAULT_REVIEW_URL = "http://127.0.0.1:18801/";
const CHANGE_SET_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;

export interface DevPilotProposal {
  changeSetId: string;
  status: string;
  fileCount: number;
}

function proposalFromResult(event: ToolProgressEvent): DevPilotProposal | null {
  if (event.name !== "devpilot_propose_changeset" || event.phase !== "end") return null;

  let value: unknown = event.result;
  if (typeof value === "string") {
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const changeSetId = record.change_set_id;
  if (typeof changeSetId !== "string" || !CHANGE_SET_ID_PATTERN.test(changeSetId)) return null;
  const files = record.files;
  return {
    changeSetId,
    status: typeof record.status === "string" ? record.status : "planned",
    fileCount: Array.isArray(files) ? files.length : 0,
  };
}

export function extractDevPilotProposals(messages: UIMessage[]): DevPilotProposal[] {
  const proposals = new Map<string, DevPilotProposal>();
  for (const message of messages) {
    for (const event of message.toolEvents ?? []) {
      const proposal = proposalFromResult(event);
      if (proposal) proposals.set(proposal.changeSetId, proposal);
    }
  }
  return [...proposals.values()];
}

function reviewUrl(changeSetId: string): string {
  const configured = import.meta.env.VITE_DEVPILOT_REVIEW_URL;
  const raw = typeof configured === "string" && configured.trim()
    ? configured
    : DEFAULT_REVIEW_URL;
  try {
    const target = new URL(raw);
    if (
      target.protocol !== "http:"
      || !["127.0.0.1", "localhost", "[::1]", "::1"].includes(target.hostname)
    ) return `${DEFAULT_REVIEW_URL}?change_set_id=${encodeURIComponent(changeSetId)}`;
    target.searchParams.set("change_set_id", changeSetId);
    return target.toString();
  } catch {
    return `${DEFAULT_REVIEW_URL}?change_set_id=${encodeURIComponent(changeSetId)}`;
  }
}

export function DevPilotProposalCards({ messages }: { messages: UIMessage[] }) {
  const proposals = extractDevPilotProposals(messages);
  if (!proposals.length) return null;

  return (
    <div className="mt-3 flex flex-col gap-2" data-testid="devpilot-proposal-cards">
      {proposals.map((proposal) => (
        <div
          className={cn(
            "flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-500/30",
            "bg-amber-500/5 px-3 py-2.5 text-sm",
          )}
          key={proposal.changeSetId}
        >
          <div className="flex min-w-0 items-center gap-2">
            <ShieldCheck className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden />
            <span className="min-w-0 truncate">
              DevPilot 变更提案 · {proposal.fileCount} 个文件 · 待人工审批
            </span>
          </div>
          <a
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-amber-600 px-3 py-1.5",
              "font-medium text-white transition-colors hover:bg-amber-700",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            )}
            data-testid="devpilot-approval-link"
            href={reviewUrl(proposal.changeSetId)}
            rel="noreferrer"
            target="_blank"
          >
            打开审批页面
            <ExternalLink className="h-3.5 w-3.5" aria-hidden />
          </a>
        </div>
      ))}
    </div>
  );
}
