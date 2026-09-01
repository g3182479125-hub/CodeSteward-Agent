import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  DevPilotProposalCards,
  extractDevPilotProposals,
} from "@/components/thread/DevPilotProposalCard";
import type { UIMessage } from "@/lib/types";

function proposalMessage(result: unknown): UIMessage {
  return {
    id: "proposal-trace",
    role: "tool",
    kind: "trace",
    content: "devpilot_propose_changeset()",
    createdAt: 1,
    toolEvents: [{
      phase: "end",
      call_id: "proposal-call",
      name: "devpilot_propose_changeset",
      result,
    }],
  };
}

describe("DevPilot proposal card", () => {
  it("extracts a JSON tool result and renders a local review link", () => {
    const message = proposalMessage(JSON.stringify({
      change_set_id: "cs-test-123",
      status: "planned",
      files: [{ relative_path: "src/index.js" }],
    }));

    expect(extractDevPilotProposals([message])).toEqual([{
      changeSetId: "cs-test-123",
      status: "planned",
      fileCount: 1,
    }]);
    render(<DevPilotProposalCards messages={[message]} />);
    expect(screen.getByText("打开审批页面").closest("a")).toHaveAttribute(
      "href",
      "http://127.0.0.1:18801/?change_set_id=cs-test-123",
    );
  });

  it("ignores malformed or unsafe tool results", () => {
    expect(extractDevPilotProposals([proposalMessage("not-json")])).toEqual([]);
    expect(extractDevPilotProposals([proposalMessage({ change_set_id: "https://evil.invalid" })])).toEqual([]);
  });
});
