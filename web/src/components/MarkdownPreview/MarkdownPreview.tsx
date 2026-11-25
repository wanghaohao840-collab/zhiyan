import { Check, Copy } from "lucide-react";
import {
  Children,
  cloneElement,
  isValidElement,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";

import { useToast } from "../ui";
import "./markdown-preview.css";

export interface InlineCitationRef {
  index: number;
  citationId: string;
  label?: string;
}

export interface MarkdownPreviewProps {
  markdown: string;
  citations?: InlineCitationRef[];
  onCitationClick?: (citationId: string, trigger: HTMLButtonElement) => void;
}

// NOTE: 严格基于 rehype-sanitize defaultSchema 仅放行数学公式与代码块语言 className，保持零 XSS
const SANITIZE_SCHEMA = {
  ...defaultSchema,
  attributes: {
    ...defaultSchema.attributes,
    code: [
      ...(defaultSchema.attributes?.code ?? []),
      ["className", /^language-[\w-]+$/, "math-inline", "math-display"],
    ],
    span: [
      ...(defaultSchema.attributes?.span ?? []),
      ["className", "math-inline", "math-display"],
    ],
    div: [
      ...(defaultSchema.attributes?.div ?? []),
      ["className", "math", "math-display"],
    ],
  },
};

function isExternalHref(href: string | undefined): boolean {
  if (!href) return false;
  try {
    const url = new URL(href, window.location.origin);
    return (
      (url.protocol === "http:" || url.protocol === "https:") &&
      url.origin !== window.location.origin
    );
  } catch {
    return false;
  }
}

function isSafeHref(href: string): boolean {
  if (!href || href.startsWith("//")) return false;
  try {
    const url = new URL(href, window.location.origin);
    return (
      (url.protocol === "http:" || url.protocol === "https:") &&
      !url.username &&
      !url.password
    );
  } catch {
    return href.startsWith("/") || href.startsWith("#") || href.startsWith("?");
  }
}

function extractText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return extractText(node.props.children);
  }
  return "";
}

function extractLanguage(children: ReactNode): string | undefined {
  const first = Children.toArray(children)[0];
  if (!isValidElement<{ className?: string }>(first)) return undefined;
  const match = first.props.className?.match(/language-([\w-]+)/i);
  return match?.[1];
}

function CodeBlock({ children }: { children: ReactNode }) {
  const toast = useToast();
  const [copied, setCopied] = useState(false);
  const language = extractLanguage(children);

  async function handleCopy() {
    const raw = extractText(children).replace(/\n$/, "");
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(raw);
      setCopied(true);
      toast.push({ message: "代码已复制到剪贴板", tone: "success" });
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      toast.push({ message: "复制失败，请手动选中代码", tone: "danger" });
    }
  }

  return (
    <div className="markdown-preview__code">
      <div className="markdown-preview__code-bar">
        <span className="markdown-preview__code-lang">{language ?? "code"}</span>
        <button
          type="button"
          className={`markdown-preview__copy${copied ? " is-copied" : ""}`}
          aria-label={copied ? "代码已复制" : "复制代码"}
          onClick={() => void handleCopy()}
        >
          {copied ? (
            <Check size={13} strokeWidth={1.8} aria-hidden="true" />
          ) : (
            <Copy size={13} strokeWidth={1.6} aria-hidden="true" />
          )}
          <span>{copied ? "已复制" : "复制"}</span>
        </button>
      </div>
      <pre>{children}</pre>
    </div>
  );
}

function injectInlineCitations(
  node: ReactNode,
  citations: InlineCitationRef[],
  onCitationClick: (citationId: string, trigger: HTMLButtonElement) => void,
): ReactNode {
  if (!citations.length) return node;
  return Children.map(node, (child, childIndex) => {
    if (typeof child === "string") {
      const parts: ReactNode[] = [];
      const regex = /\[([^\]]+)\]/g;
      let lastIndex = 0;
      let match: RegExpExecArray | null;
      while ((match = regex.exec(child)) !== null) {
        const token = match[1].trim();
        const found = citations.find(
          (item) => String(item.index) === token || item.citationId === token,
        );
        if (!found) continue;
        if (match.index > lastIndex) {
          parts.push(child.slice(lastIndex, match.index));
        }
        parts.push(
          <button
            key={`${childIndex}-${match.index}-${found.citationId}`}
            type="button"
            className="qa-inline-citation tabular-nums"
            title={found.label ? `定位引用 [${found.index}]：${found.label}` : `定位引用 [${found.index}]`}
            aria-label={`查看正文引用 ${found.index}`}
            onClick={(event) => onCitationClick(found.citationId, event.currentTarget)}
          >
            {found.index}
          </button>,
        );
        lastIndex = regex.lastIndex;
      }
      if (!parts.length) return child;
      if (lastIndex < child.length) parts.push(child.slice(lastIndex));
      return parts;
    }
    if (isValidElement<{ children?: ReactNode }>(child)) {
      const tag = typeof child.type === "string" ? child.type : "";
      if (tag === "code" || tag === "pre" || tag === "a" || tag === "button") {
        return child;
      }
      if (child.props.children !== undefined) {
        return cloneElement(child as ReactElement<{ children?: ReactNode }>, {
          children: injectInlineCitations(child.props.children, citations, onCitationClick),
        });
      }
    }
    return child;
  });
}

export function MarkdownPreview({
  markdown,
  citations,
  onCitationClick,
}: MarkdownPreviewProps) {
  const hasInlineCitations = Boolean(citations?.length && onCitationClick);
  const wrapText = (children: ReactNode) =>
    hasInlineCitations
      ? injectInlineCitations(children, citations!, onCitationClick!)
      : children;

  return (
    <div className="markdown-preview">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[
          [rehypeSanitize, SANITIZE_SCHEMA],
          [rehypeKatex, { throwOnError: false, strict: false }],
          [rehypeHighlight, { detect: true }],
        ]}
        urlTransform={(url) => (isSafeHref(url) ? url : "")}
        components={{
          a: ({ href, children, ...props }) =>
            isSafeHref(href ?? "") ? (
              <a
                {...props}
                href={href}
                rel={isExternalHref(href) ? "noopener noreferrer" : undefined}
              >
                {children}
              </a>
            ) : (
              <>{children}</>
            ),
          p: ({ children }) => <p>{wrapText(children)}</p>,
          li: ({ children }) => <li>{wrapText(children)}</li>,
          blockquote: ({ children }) => <blockquote>{wrapText(children)}</blockquote>,
          td: ({ children }) => <td>{wrapText(children)}</td>,
          pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
          table: ({ children }) => (
            <div className="markdown-preview__table">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
