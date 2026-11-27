import type { ReactNode } from 'react';

/**
 * 渲染行内 Markdown（粗体与反引号代码），纯 React 节点构造，绝不使用 dangerouslySetInnerHTML。
 */
function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const tokenRe = /(`[^`\r\n]+`|\*\*[^*\r\n]+\*\*)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let idx = 0;

  while ((match = tokenRe.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }
    const token = match[0];
    if (token.startsWith('`') && token.endsWith('`')) {
      nodes.push(
        <code key={`${keyPrefix}-c-${idx++}`} className="ch-inline-code">
          {token.slice(1, -1)}
        </code>,
      );
    } else if (token.startsWith('**') && token.endsWith('**')) {
      nodes.push(
        <strong key={`${keyPrefix}-b-${idx++}`}>
          {token.slice(2, -2)}
        </strong>,
      );
    }
    lastIndex = match.index + token.length;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }
  return nodes;
}

interface SafeMarkdownProps {
  text: string;
  className?: string;
}

/**
 * 轻量级安全 Markdown 块级渲染器：支持标题、无序/有序列表、围栏代码块与段落。
 */
export function SafeMarkdown({ text, className }: SafeMarkdownProps) {
  const lines = text.replace(/\r\n/g, '\n').split('\n');
  const blocks: ReactNode[] = [];
  let i = 0;
  let blockKey = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i += 1;
      continue;
    }

    // 围栏代码块
    if (trimmed.startsWith('```')) {
      const codeLines: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        codeLines.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      blocks.push(
        <pre key={`md-code-${blockKey++}`} className="ch-md-codeblock">
          <code>{codeLines.join('\n')}</code>
        </pre>,
      );
      continue;
    }

    // 标题行
    const headingMatch = /^(#{1,3})\s+(.+)$/.exec(trimmed);
    if (headingMatch) {
      const level = headingMatch[1].length;
      blocks.push(
        <div key={`md-h-${blockKey++}`} className={`ch-md-heading ch-md-h${level}`}>
          {renderInline(headingMatch[2], `h-${blockKey}`)}
        </div>,
      );
      i += 1;
      continue;
    }

    // 无序列表
    if (/^[-*•]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*•]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^[-*•]\s+/, ''));
        i += 1;
      }
      blocks.push(
        <ul key={`md-ul-${blockKey++}`} className="ch-md-list">
          {items.map((item, idx) => (
            <li key={idx}>{renderInline(item, `ul-${blockKey}-${idx}`)}</li>
          ))}
        </ul>,
      );
      continue;
    }

    // 有序列表
    if (/^\d+\.\s+/.test(trimmed)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^\d+\.\s+/, ''));
        i += 1;
      }
      blocks.push(
        <ol key={`md-ol-${blockKey++}`} className="ch-md-list ch-md-olist">
          {items.map((item, idx) => (
            <li key={idx}>{renderInline(item, `ol-${blockKey}-${idx}`)}</li>
          ))}
        </ol>,
      );
      continue;
    }

    // 普通段落（合并连续非空普通文本行）
    const paraLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !lines[i].trim().startsWith('```') &&
      !/^(#{1,3})\s+/.test(lines[i].trim()) &&
      !/^[-*•]\s+/.test(lines[i].trim()) &&
      !/^\d+\.\s+/.test(lines[i].trim())
    ) {
      paraLines.push(lines[i]);
      i += 1;
    }
    const paraText = paraLines.join('\n');
    blocks.push(
      <p key={`md-p-${blockKey++}`} className="ch-md-para">
        {renderInline(paraText, `p-${blockKey}`)}
      </p>,
    );
  }

  return <div className={className ? `ch-md ${className}` : 'ch-md'}>{blocks}</div>;
}
