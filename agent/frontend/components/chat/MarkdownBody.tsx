import { Fragment, type ReactNode } from "react";

import { cn } from "@/lib/utils/cn";

const INCIDENT_ID = /\b(IN\d{14,})\b/g;
const INLINE_CODE = /`([^`]+)`/g;
const BOLD = /\*\*([^*]+)\*\*/g;

function applyIncidentIds(text: string, keyPrefix: string): ReactNode[] {
  const parts: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const match of text.matchAll(INCIDENT_ID)) {
    const idx = match.index ?? 0;
    if (idx > last) parts.push(text.slice(last, idx));
    parts.push(
      <code
        key={`${keyPrefix}-id-${i++}`}
        className="font-mono-data rounded bg-surface-raised px-1 py-0.5 text-[0.9em]"
      >
        {match[1]}
      </code>,
    );
    last = idx + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts.length ? parts : [text];
}

function inlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  let nodes: ReactNode[] = [text];

  nodes = nodes.flatMap((node, ni) => {
    if (typeof node !== "string") return [node];
    const out: ReactNode[] = [];
    let last = 0;
    let i = 0;
    for (const match of node.matchAll(INLINE_CODE)) {
      const idx = match.index ?? 0;
      if (idx > last) {
        out.push(...applyIncidentIds(node.slice(last, idx), `${keyPrefix}-${ni}-pre-${i}`));
      }
      out.push(
        <code
          key={`${keyPrefix}-code-${ni}-${i++}`}
          className="font-mono-data rounded bg-surface-raised px-1 py-0.5 text-[0.9em]"
        >
          {match[1]}
        </code>,
      );
      last = idx + match[0].length;
    }
    if (last < node.length) {
      out.push(...applyIncidentIds(node.slice(last), `${keyPrefix}-${ni}-tail`));
    }
    return out.length ? out : applyIncidentIds(node, `${keyPrefix}-${ni}`);
  });

  nodes = nodes.flatMap((node, ni) => {
    if (typeof node !== "string") return [node];
    const out: ReactNode[] = [];
    let last = 0;
    let i = 0;
    for (const match of node.matchAll(BOLD)) {
      const idx = match.index ?? 0;
      if (idx > last) out.push(node.slice(last, idx));
      out.push(
        <strong key={`${keyPrefix}-bold-${ni}-${i++}`} className="font-semibold">
          {match[1]}
        </strong>,
      );
      last = idx + match[0].length;
    }
    if (last < node.length) out.push(node.slice(last));
    return out.length ? out : [node];
  });

  return nodes;
}

function renderBlock(block: string, index: number): ReactNode {
  const trimmed = block.trim();
  if (!trimmed) return null;

  if (trimmed.startsWith("```")) {
    const lines = trimmed.split("\n");
    const lang = lines[0]?.replace(/^```/, "").trim();
    const code = lines.slice(1, lines.at(-1)?.trim() === "```" ? -1 : undefined).join("\n");
    return (
      <pre
        key={`block-${index}`}
        className="font-mono-data overflow-x-auto rounded-md border border-border bg-background p-3 text-xs leading-relaxed"
      >
        {lang ? (
          <span className="mb-2 block text-[10px] uppercase tracking-wide text-muted">
            {lang}
          </span>
        ) : null}
        <code>{code}</code>
      </pre>
    );
  }

  if (/^[-*]\s/.test(trimmed)) {
    const items = trimmed.split("\n").filter((l) => /^[-*]\s/.test(l.trim()));
    return (
      <ul key={`block-${index}`} className="list-disc space-y-1 pl-5">
        {items.map((item, i) => (
          <li key={i} className="leading-relaxed">
            {inlineMarkdown(item.replace(/^[-*]\s+/, ""), `li-${index}-${i}`)}
          </li>
        ))}
      </ul>
    );
  }

  return (
    <p key={`block-${index}`} className="leading-relaxed">
      {inlineMarkdown(trimmed, `p-${index}`)}
    </p>
  );
}

export interface MarkdownBodyProps {
  content: string;
  className?: string;
}

export function MarkdownBody({ content, className }: MarkdownBodyProps) {
  const blocks = content.split(/\n{2,}/);
  return (
    <div className={cn("space-y-3 text-sm text-foreground", className)}>
      {blocks.map((block, i) => (
        <Fragment key={i}>{renderBlock(block, i)}</Fragment>
      ))}
    </div>
  );
}
