import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import {
  flattenTokens,
  renderCss,
  writeCss,
} from "../../scripts/design_tokens.mjs";

function token(type, value) {
  return { $type: type, $value: value };
}

function shadow(value = {}) {
  return token("shadow", {
    color: "#20262114",
    offsetX: { value: 0, unit: "px" },
    offsetY: { value: 2, unit: "px" },
    blur: { value: 12, unit: "px" },
    spread: { value: 0, unit: "px" },
    ...value,
  });
}

test("renders DTCG paths as stable CSS variables", () => {
  const css = renderCss({
    color: { brand: { 600: token("color", "#24745B") } },
    space: { 4: token("dimension", { value: 16, unit: "px" }) },
  });

  assert.match(css, /--color-brand-600: #24745B;/);
  assert.match(css, /--space-4: 16px;/);
  assert.ok(css.indexOf("--color-brand-600") < css.indexOf("--space-4"));
  assert.equal(css, ":root {\n  --color-brand-600: #24745B;\n  --space-4: 16px;\n}\n");
});

test("renders typography, motion, number, and layered shadow tokens", () => {
  const css = renderCss({
    font: {
      sans: token("fontFamily", ["Inter", "PingFang SC", "sans-serif"]),
      weight: { medium: token("fontWeight", 500) },
    },
    leading: { reading: token("number", 1.8) },
    duration: { fast: token("duration", { value: 140, unit: "ms" }) },
    ease: { standard: token("cubicBezier", [0.2, 0, 0, 1]) },
    shadow: {
      layered: token("shadow", [
        {
          color: "#2026210a",
          offsetX: { value: 0, unit: "px" },
          offsetY: { value: 1, unit: "px" },
          blur: { value: 2, unit: "px" },
          spread: { value: 0, unit: "px" },
        },
        {
          color: "#20262112",
          offsetX: { value: 0, unit: "px" },
          offsetY: { value: 8, unit: "px" },
          blur: { value: 24, unit: "px" },
          spread: { value: 0, unit: "px" },
        },
      ]),
    },
  });

  assert.match(css, /--font-sans: Inter, "PingFang SC", sans-serif;/);
  assert.match(css, /--font-weight-medium: 500;/);
  assert.match(css, /--leading-reading: 1\.8;/);
  assert.match(css, /--duration-fast: 140ms;/);
  assert.match(css, /--ease-standard: cubic-bezier\(0\.2, 0, 0, 1\);/);
  assert.match(
    css,
    /--shadow-layered: 0px 1px 2px 0px #2026210a, 0px 8px 24px 0px #20262112;/,
  );
});

test("renders shadows in standard CSS order", () => {
  assert.match(
    renderCss({ shadow: { surface: shadow() } }),
    /--shadow-surface: 0px 2px 12px 0px #20262114;/,
  );
});

test("renders multi-mode theme overrides and inset shadows deterministically", () => {
  const css = renderCss({
    color: { canvas: token("color", "#F7F6F2") },
    $modes: {
      midnight: {
        color: { canvas: token("color", "#070C15") },
        shadow: {
          glass: token("shadow", [
            {
              inset: true,
              color: "rgba(255, 255, 255, 0.06)",
              offsetX: { value: 0, unit: "px" },
              offsetY: { value: 1, unit: "px" },
              blur: { value: 0, unit: "px" },
              spread: { value: 0, unit: "px" },
            },
          ]),
        },
      },
    },
  });

  assert.match(css, /^:root \{\n  --color-canvas: #F7F6F2;\n\}\n\n:root\[data-theme="midnight"\] \{/);
  assert.match(css, /--color-canvas: #070C15;/);
  assert.match(css, /--shadow-glass: inset 0px 1px 0px 0px rgba\(255, 255, 255, 0\.06\);/);
  assert.throws(
    () => renderCss({ color: { canvas: token("color", "#fff") }, $modes: { "BAD MODE": {} } }),
    /Invalid theme mode name: BAD MODE/,
  );
});

test("rejects duplicate flattened names with a token path", () => {
  assert.throws(
    () => flattenTokens({ color: { brand: token("color", "#fff") }, "color.brand": token("color", "#000") }),
    /Duplicate CSS token name: color\.brand/,
  );
});

test("rejects invalid units, malformed durations, and unknown types with token paths", () => {
  assert.throws(
    () => renderCss({ space: { bad: token("dimension", { value: 1, unit: "em" }) } }),
    /Unsupported or invalid token: space\.bad/,
  );
  assert.throws(
    () => renderCss({ duration: { bad: token("duration", "100ms") } }),
    /Unsupported or invalid token: duration\.bad/,
  );
  assert.throws(
    () => renderCss({ mystery: { bad: token("customType", "x") } }),
    /Unsupported or invalid token: mystery\.bad/,
  );
});

test("rejects non-finite dimensions and shadow fields with token paths", () => {
  for (const value of [NaN, Infinity, -Infinity]) {
    assert.throws(
      () => renderCss({ space: { invalid: token("dimension", { value, unit: "px" }) } }),
      /Unsupported or invalid token: space\.invalid/,
    );
  }
  for (const field of ["offsetX", "offsetY", "blur", "spread"]) {
    assert.throws(
      () => renderCss({ shadow: { invalid: shadow({ [field]: { value: NaN, unit: "px" } }) } }),
      /Unsupported or invalid token: shadow\.invalid/,
    );
  }
});

test("rejects tokens that are missing $value with their path", () => {
  assert.throws(
    () => renderCss({ color: { missing: { $type: "color" } } }),
    /Unsupported or invalid token: color\.missing/,
  );
});

test("rejects invalid or incomplete shadows with token paths", () => {
  assert.throws(
    () => renderCss({ shadow: { missing: shadow({ color: undefined }) } }),
    /Unsupported or invalid token: shadow\.missing/,
  );
  assert.throws(
    () => renderCss({ shadow: { unit: shadow({ blur: { value: 1, unit: "em" } }) } }),
    /Unsupported or invalid token: shadow\.unit/,
  );
});

test("CLI --check accepts matching LF or CRLF CSS and rejects stale output", () => {
  const directory = mkdtempSync(join(tmpdir(), "design-tokens-"));
  const input = join(directory, "tokens.json");
  const output = join(directory, "tokens.css");
  const source = JSON.stringify({ color: { brand: token("color", "#24745B") } });

  try {
    writeFileSync(input, source, "utf8");
    writeCss(input, output);
    const success = spawnSync(process.execPath, ["scripts/design_tokens.mjs", "--check", input, output]);
    assert.equal(success.status, 0, success.stderr.toString());
    assert.match(readFileSync(output, "utf8"), /--color-brand: #24745B;/);

    const crlf = readFileSync(output, "utf8").replaceAll("\n", "\r\n");
    writeFileSync(output, crlf, "utf8");
    const windows = spawnSync(process.execPath, ["scripts/design_tokens.mjs", "--check", input, output]);
    assert.equal(windows.status, 0, windows.stderr.toString());

    writeFileSync(output, "stale\n", "utf8");
    const stale = spawnSync(process.execPath, ["scripts/design_tokens.mjs", "--check", input, output]);
    assert.notEqual(stale.status, 0);
    assert.match(stale.stderr.toString(), /Generated CSS is stale:/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
