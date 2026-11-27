/**
 * 监控面板时间、耗时与 Windows file:// URI 格式化工具
 */

/**
 * 将 ISO 时间字符串格式化为类似 "11-25 21:33" 的紧凑中文界面时间戳
 */
export function formatShortTimestamp(iso?: string | null): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const dd = String(date.getDate()).padStart(2, '0');
  const hh = String(date.getHours()).padStart(2, '0');
  const min = String(date.getMinutes()).padStart(2, '0');
  return `${mm}-${dd} ${hh}:${min}`;
}

/**
 * 将毫秒数格式化为类似 "16分35秒" / "4.2秒" / "0.08s"
 */
export function formatDurationZh(ms?: number | null): string {
  if (ms === undefined || ms === null || ms < 0) return '';
  const totalSec = ms / 1000;
  if (totalSec < 60) {
    return totalSec < 10 ? `${totalSec.toFixed(1)}秒` : `${Math.round(totalSec)}秒`;
  }
  const mins = Math.floor(totalSec / 60);
  const secs = Math.round(totalSec % 60);
  return `${mins}分${String(secs).padStart(2, '0')}秒`;
}

/**
 * 命令单条耗时紧凑格式（对应截图右侧的 0.01s / 0.26s）
 */
export function formatCommandSeconds(ms?: number | null): string {
  if (ms === undefined || ms === null || ms < 0) return '';
  const sec = ms / 1000;
  if (sec < 10) return `${sec.toFixed(2)}s`;
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const mins = Math.floor(sec / 60);
  const rem = Math.round(sec % 60);
  return `${mins}m${rem}s`;
}

/**
 * 将本地绝对路径转为 Antigravity toggle-aux-pane 可接受的 file:/// URI
 */
export function toFileUri(localPath: string): string {
  const normalized = localPath.replace(/\\/g, '/');
  if (normalized.startsWith('file://')) return normalized;
  if (/^[A-Za-z]:\//.test(normalized)) {
    return `file:///${encodeURI(normalized)}`;
  }
  return `file://${encodeURI(normalized.startsWith('/') ? normalized : `/${normalized}`)}`;
}

/**
 * 从完整路径提取文件名或最后两级路径，用于紧凑徽章展示
 */
export function shortPathLabel(rawPath: string): string {
  const parts = rawPath.replace(/\\/g, '/').split('/').filter(Boolean);
  if (parts.length <= 2) return parts.join('/');
  return parts.slice(-2).join('/');
}
