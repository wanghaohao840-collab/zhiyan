import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MarkdownPreview } from "./MarkdownPreview";

describe("MarkdownPreview", () => {
  it("renders GFM while dropping hostile HTML, protocols and event attributes", () => {
    render(
      <MarkdownPreview
        markdown={
          '# Safe\n\n- [x] done\n\n<script>alert(1)</script><iframe src="javascript:alert(1)" onload="alert(2)"></iframe>\n\n[bad](javascript:alert(1))'
        }
      />,
    );
    expect(screen.getByRole("heading", { name: "Safe" })).toBeVisible();
    expect(screen.getByRole("checkbox")).toBeChecked();
    expect(screen.queryByText("alert(1)")).not.toBeInTheDocument();
    expect(document.querySelector("script, iframe, [onload]")).toBeNull();
    expect(document.querySelector("a[href^='javascript']")).toBeNull();
  });

  it("marks external links noopener while retaining safe relative links", () => {
    render(
      <MarkdownPreview
        markdown={
          "[external](https://example.com) [relative](/notes/1) [credentialized](https://user:secret@example.com/private) [protocol-relative](//example.com)"
        }
      />,
    );
    expect(screen.getByRole("link", { name: "external" })).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
    expect(screen.getByRole("link", { name: "relative" })).not.toHaveAttribute("rel");
    expect(screen.queryByRole("link", { name: "credentialized" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "protocol-relative" })).not.toBeInTheDocument();
  });

  it("renders KaTeX math formulas and code block language header with one-click copy", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(
      <MarkdownPreview
        markdown={"能量公式 $E = mc^2$。\n\n```python\nanswer = 42\n```"}
      />,
    );

    expect(document.querySelector(".katex")).not.toBeNull();
    expect(screen.getByText("python")).toBeVisible();
    const copyButton = screen.getByRole("button", { name: "复制代码" });
    fireEvent.click(copyButton);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("answer = 42"));
    expect(screen.getByRole("button", { name: "代码已复制" })).toBeVisible();
  });

  it("turns prose citation markers into clickable superscript badges without altering code blocks", () => {
    const onCitationClick = vi.fn();
    render(
      <MarkdownPreview
        markdown={"结论见来源 [1]。\n\n```python\narr = [1]\n```"}
        citations={[{ index: 1, citationId: "cit-1", label: "论文.pdf" }]}
        onCitationClick={onCitationClick}
      />,
    );

    const badges = screen.getAllByRole("button", { name: "查看正文引用 1" });
    expect(badges).toHaveLength(1);
    fireEvent.click(badges[0]);
    expect(onCitationClick).toHaveBeenCalledWith("cit-1", badges[0]);
  });
});
