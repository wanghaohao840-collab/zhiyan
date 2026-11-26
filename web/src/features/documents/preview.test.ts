import { describe, expect, it } from "vitest";
import {
  sliceCodePoints,
  splitHighlightedContext,
  supportsOriginalContentPreview,
} from "./preview";

describe("document preview contracts", () => {
  it("recognizes formats supporting original byte-stream preview vs chunk-only formats", () => {
    expect(supportsOriginalContentPreview(".pdf")).toBe(true);
    expect(supportsOriginalContentPreview(".MD")).toBe(true);
    expect(supportsOriginalContentPreview(".markdown")).toBe(true);
    expect(supportsOriginalContentPreview(".txt")).toBe(true);
    expect(supportsOriginalContentPreview(".docx")).toBe(false);
    expect(supportsOriginalContentPreview("")).toBe(false);
  });

  it("slices highlight offsets by Unicode code point without shifting on surrogate pairs", () => {
    // 后端 Python: context = "甲😀𝐀目标。乙" => highlight_start=3, highlight_end=5
    const context = "甲😀𝐀目标。乙";
    expect(sliceCodePoints(context, 3, 5)).toBe("目标");
    expect(splitHighlightedContext(context, 3, 5)).toEqual({
      before: "甲😀𝐀",
      highlight: "目标",
      after: "。乙",
      hasHighlight: true,
    });
  });

  it("degrades safely when legacy documents omit offsets or return out-of-range indices", () => {
    expect(splitHighlightedContext("旧摘录原文", null, null)).toEqual({
      before: "旧摘录原文",
      highlight: "",
      after: "",
      hasHighlight: false,
    });
    expect(splitHighlightedContext("短文", 0, 99)).toEqual({
      before: "短文",
      highlight: "",
      after: "",
      hasHighlight: false,
    });
  });
});
