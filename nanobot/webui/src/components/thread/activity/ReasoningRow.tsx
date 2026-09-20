import { useEffect, useId, useRef, useState } from "react";
import { Check, CircleDashed } from "lucide-react";

import { cn } from "@/lib/utils";

import { ActivityStep } from "./ActivityStep";
import { compactReasoningPreview } from "./reasoning-preview";

function chineseReasoningPreview(text: string, streaming: boolean): string {
  const preview = compactReasoningPreview(text);
  // Reasoning deltas come directly from the model and are not safe to machine
  // translate in the browser.  Show the stable Chinese progress summary until
  // the model supplies Chinese narration; this avoids mixed-language streaming
  // while preserving technical detail in the tool rows below.
  return /[\u3400-\u9fff]/.test(preview)
    ? preview
    : streaming ? "正在分析下一步" : "已完成本步分析";
}

export function ReasoningRow({
  text,
  streaming,
  className,
}: {
  text: string;
  streaming: boolean;
  className?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const detailsId = `reasoning-details-${useId().replace(/:/g, "")}`;
  const preview = chineseReasoningPreview(text, streaming);
  return (
    <ActivityStep
      marker={<ReasoningMarker streaming={streaming} />}
      active={streaming}
      tone={streaming ? "active" : "success"}
      label={preview}
      labelClassName="italic text-muted-foreground/78"
      contentClassName="overflow-hidden"
      detailsId={detailsId}
      expanded={expanded}
      onToggle={() => setExpanded((value) => !value)}
      details={
        <div
          data-testid="reasoning-details"
          className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-border/45 bg-muted/20 px-2.5 py-2 text-[12px] leading-5 text-muted-foreground/82"
        >
          {text || "暂无详细思考内容"}
        </div>
      }
      className={className}
    />
  );
}

function ReasoningMarker({ streaming }: { streaming: boolean }) {
  const wasStreamingRef = useRef(streaming);
  const [justCompleted, setJustCompleted] = useState(false);

  useEffect(() => {
    if (wasStreamingRef.current && !streaming) {
      setJustCompleted(true);
      const timeout = window.setTimeout(() => setJustCompleted(false), 300);
      wasStreamingRef.current = streaming;
      return () => window.clearTimeout(timeout);
    }
    wasStreamingRef.current = streaming;
    return undefined;
  }, [streaming]);

  if (streaming) {
    return (
      <CircleDashed
        data-testid="activity-reasoning-marker"
        data-state="thinking"
        className="h-3.5 w-3.5 shrink-0 animate-spin text-muted-foreground/55"
        strokeWidth={1.8}
        aria-hidden
      />
    );
  }
  return (
    <span
      data-testid="activity-reasoning-marker"
      data-state="done"
      className={cn(
        "grid h-3.5 w-3.5 shrink-0 place-items-center rounded-full border border-emerald-500/28 text-emerald-500/78",
        "bg-emerald-500/[0.035] transition-[border-color,background-color,box-shadow,transform] duration-300 ease-out",
        justCompleted
          && "animate-in fade-in-0 zoom-in-75 shadow-[0_0_0_3px_rgba(16,185,129,0.10)] motion-reduce:animate-none",
      )}
      aria-hidden
    >
      <Check
        className={cn(
          "h-2.5 w-2.5 stroke-[2.4]",
          justCompleted && "animate-in fade-in-0 zoom-in-50 duration-300 motion-reduce:animate-none",
        )}
      />
    </span>
  );
}
