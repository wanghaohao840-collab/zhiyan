/**
 * 文档原文与片段上下文预览契约工具。
 *
 * NOTE: 后端 /api/v1/documents/{id}/chunks/{chunk_id} 返回的 highlight_start/highlight_end
 * 是 Python Unicode code point 索引而非 JS UTF-16 code unit 索引；必须经由本模块按码点切片，
 * 避免含 emoji（😀）或数学扩展符号（𝐀 U+1D400）的上下文发生高亮错位。
 */

export const ORIGINAL_CONTENT_PREVIEW_SUFFIXES = new Set([
  ".pdf",
  ".txt",
  ".md",
  ".markdown",
]);

export interface HighlightedContextSegments {
  before: string;
  highlight: string;
  after: string;
  hasHighlight: boolean;
}

/**
 * 判断文档后缀是否支持 /api/v1/documents/{id}/content 原文字节流预览。
 */
export function supportsOriginalContentPreview(fileSuffix: string | null | undefined): boolean {
  if (!fileSuffix) return false;
  return ORIGINAL_CONTENT_PREVIEW_SUFFIXES.has(fileSuffix.trim().toLowerCase());
}

/**
 * 按 Unicode code point 区间 [start, end) 切片字符串，兼容代理对字符。
 */
export function sliceCodePoints(text: string, start: number, end?: number): string {
  const codePoints = Array.from(text);
  return codePoints.slice(start, end).join("");
}

/**
 * 将后端返回的 context_text 与 Unicode 码点高亮区间拆分为前置/高亮/后置三段。
 * 当旧文档未记录偏移或索引越界时安全降级为无高亮纯上下文。
 */
export function splitHighlightedContext(
  contextText: string,
  highlightStart: number | null | undefined,
  highlightEnd: number | null | undefined,
): HighlightedContextSegments {
  if (
    typeof highlightStart !== "number" ||
    typeof highlightEnd !== "number" ||
    !Number.isSafeInteger(highlightStart) ||
    !Number.isSafeInteger(highlightEnd)
  ) {
    return { before: contextText, highlight: "", after: "", hasHighlight: false };
  }
  const codePoints = Array.from(contextText);
  if (
    highlightStart < 0 ||
    highlightEnd <= highlightStart ||
    highlightEnd > codePoints.length
  ) {
    return { before: contextText, highlight: "", after: "", hasHighlight: false };
  }
  return {
    before: codePoints.slice(0, highlightStart).join(""),
    highlight: codePoints.slice(highlightStart, highlightEnd).join(""),
    after: codePoints.slice(highlightEnd).join(""),
    hasHighlight: true,
  };
}
