/**
 * 基于宿主实际渲染背景色与文字色亮度推导真实明暗极性。
 *
 * NOTE: 当 Antigravity 使用自定义浅色/护眼暖色主题（如米黄羊皮纸底色）时，
 * 宿主上报的 `ctx.theme` 仍可能为 `"dark"`。若直接按 `ctx.theme` 启用暗色样式，
 * 会导致深黑气泡叠加浅色主题的黑字，严重降低肉眼可读性。
 */

export type EffectiveTheme = 'light' | 'dark';

interface ParsedRgba {
  r: number;
  g: number;
  b: number;
  a: number;
}

/**
 * 解析浏览器计算后的常见 CSS 颜色字符串为 0-255 RGB 与 0-1 Alpha。
 */
export function parseCssColorToRgba(input: string): ParsedRgba | null {
  const value = input.trim().toLowerCase();
  if (!value || value === 'transparent') return null;

  // rgb(r, g, b) / rgba(r, g, b, a) / rgb(r g b / a)
  const rgbMatch =
    /^rgba?\(\s*([0-9.]+%?)[,\s]+([0-9.]+%?)[,\s]+([0-9.]+%?)(?:\s*[,/]\s*([0-9.]+%?))?\s*\)$/.exec(
      value,
    );
  if (rgbMatch) {
    const parseChannel = (raw: string): number =>
      raw.endsWith('%') ? (parseFloat(raw) / 100) * 255 : parseFloat(raw);
    const parseAlpha = (raw?: string): number => {
      if (!raw) return 1;
      return raw.endsWith('%') ? parseFloat(raw) / 100 : parseFloat(raw);
    };
    return {
      r: Math.max(0, Math.min(255, parseChannel(rgbMatch[1]))),
      g: Math.max(0, Math.min(255, parseChannel(rgbMatch[2]))),
      b: Math.max(0, Math.min(255, parseChannel(rgbMatch[3]))),
      a: Math.max(0, Math.min(1, parseAlpha(rgbMatch[4]))),
    };
  }

  // color(srgb r g b) / color(srgb r g b / a)
  const srgbMatch =
    /^color\(\s*srgb\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)(?:\s*\/\s*([0-9.]+%?))?\s*\)$/.exec(
      value,
    );
  if (srgbMatch) {
    const parseAlpha = (raw?: string): number => {
      if (!raw) return 1;
      return raw.endsWith('%') ? parseFloat(raw) / 100 : parseFloat(raw);
    };
    return {
      r: Math.max(0, Math.min(255, parseFloat(srgbMatch[1]) * 255)),
      g: Math.max(0, Math.min(255, parseFloat(srgbMatch[2]) * 255)),
      b: Math.max(0, Math.min(255, parseFloat(srgbMatch[3]) * 255)),
      a: Math.max(0, Math.min(1, parseAlpha(srgbMatch[4]))),
    };
  }

  // #rgb / #rrggbb / #rrggbbaa
  const hexMatch = /^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/.exec(value);
  if (hexMatch) {
    const hex = hexMatch[1];
    if (hex.length === 3) {
      return {
        r: parseInt(hex[0] + hex[0], 16),
        g: parseInt(hex[1] + hex[1], 16),
        b: parseInt(hex[2] + hex[2], 16),
        a: 1,
      };
    }
    return {
      r: parseInt(hex.slice(0, 2), 16),
      g: parseInt(hex.slice(2, 4), 16),
      b: parseInt(hex.slice(4, 6), 16),
      a: hex.length === 8 ? parseInt(hex.slice(6, 8), 16) / 255 : 1,
    };
  }

  return null;
}

/**
 * 计算 WCAG 2.x 相对亮度（0 为纯黑，1 为纯白）。
 */
export function computeRelativeLuminance(r: number, g: number, b: number): number {
  const linearize = (channel: number): number => {
    const normalized = channel / 255;
    return normalized <= 0.03928
      ? normalized / 12.92
      : Math.pow((normalized + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * linearize(r) + 0.7152 * linearize(g) + 0.0722 * linearize(b);
}

/**
 * 通过隐藏探针节点测量宿主 CSS 变量在当前文档中的实际计算亮度。
 */
function measureCssVariableLuminance(varName: string): number | null {
  if (typeof document === 'undefined' || !document.documentElement) {
    return null;
  }

  const rawValue = getComputedStyle(document.documentElement)
    .getPropertyValue(varName)
    .trim();
  if (!rawValue) {
    return null;
  }

  // 直接可解析的字面量无需触碰 DOM
  const direct = parseCssColorToRgba(rawValue);
  if (direct) {
    return direct.a >= 0.5 ? computeRelativeLuminance(direct.r, direct.g, direct.b) : null;
  }

  const mountTarget = document.body || document.documentElement;
  const probe = document.createElement('span');
  probe.style.position = 'fixed';
  probe.style.visibility = 'hidden';
  probe.style.pointerEvents = 'none';
  probe.style.backgroundColor = `var(${varName})`;
  mountTarget.appendChild(probe);
  try {
    const computed = getComputedStyle(probe).backgroundColor;
    const parsed = parseCssColorToRgba(computed);
    if (!parsed || parsed.a < 0.5) {
      return null;
    }
    return computeRelativeLuminance(parsed.r, parsed.g, parsed.b);
  } finally {
    probe.remove();
  }
}

/**
 * 综合宿主实际背景色与前景色亮度判定真实明暗模式，仅在无法测量时回退到宿主上报值。
 */
export function resolveEffectiveTheme(reportedTheme?: EffectiveTheme): EffectiveTheme {
  const bgLum = measureCssVariableLuminance('--color-background-primary');
  const textLum = measureCssVariableLuminance('--color-text-primary');

  if (bgLum !== null && textLum !== null) {
    if (bgLum > textLum + 0.08) return 'light';
    if (textLum > bgLum + 0.08) return 'dark';
  }

  if (bgLum !== null) {
    return bgLum >= 0.3 ? 'light' : 'dark';
  }

  return reportedTheme === 'dark' ? 'dark' : 'light';
}
