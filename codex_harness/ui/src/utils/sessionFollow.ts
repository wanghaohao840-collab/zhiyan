import type { DashboardSnapshot, SessionDetail, SessionSummary } from '../types/harness';

export type SessionSelectionMode = 'auto' | 'manual';

export interface SnapshotSelectionParams {
  snapshot: DashboardSnapshot;
  currentSessionId: string | null;
  selectionMode: SessionSelectionMode;
  autoFollow: boolean;
  searchActive: boolean;
}

export interface BackgroundRunningInfo {
  primarySession: SessionSummary;
  runningCount: number;
}

export interface DetailCoordinatorOptions {
  fetchDetail: (sessionId: string) => Promise<SessionDetail>;
  getSelectedSessionId: () => string | null;
  onDetailLoaded: (requestedSessionId: string, detail: SessionDetail) => void;
  onLoadingChange: (loading: boolean) => void;
  onError: (sessionId: string, message: string) => void;
  maxCacheSize?: number;
}

export interface SessionDetailCoordinator {
  getCached: (sessionId: string | null) => SessionDetail | null;
  request: (sessionId: string | null, showSpinner?: boolean) => Promise<void>;
  dispose: () => void;
}

function activityTimestamp(session: SessionSummary): number {
  return session.latest_activity_ts ?? session.updated_ts ?? 0;
}

/**
 * 在项目分组快照中按 session_id（及回退别名）查找对应主会话或子代理摘要。
 */
export function findSessionSummaryById(
  snapshot: DashboardSnapshot | null,
  sessionId: string | null,
): SessionSummary | null {
  if (!snapshot || !sessionId) return null;

  let aliasMatch: SessionSummary | null = null;
  for (const group of snapshot.project_groups) {
    for (const root of group.sessions) {
      if (root.session_id === sessionId) return root;
      if (!aliasMatch && (root.run_id === sessionId || root.codex_thread_id === sessionId)) {
        aliasMatch = root;
      }
      for (const child of root.child_sessions ?? []) {
        if (child.session_id === sessionId) return child;
        if (!aliasMatch && (child.run_id === sessionId || child.codex_thread_id === sessionId)) {
          aliasMatch = child;
        }
      }
    }
  }
  return aliasMatch;
}

/**
 * 列出当前快照中全部运行中的主会话，按最近活跃时间倒序排列。
 */
export function listRunningRootSessions(snapshot: DashboardSnapshot | null): SessionSummary[] {
  if (!snapshot) return [];
  const running = snapshot.project_groups
    .flatMap((group) => group.sessions)
    .filter((session) => session.status === 'running');
  running.sort((a, b) => activityTimestamp(b) - activityTimestamp(a));
  return running;
}

/**
 * 判断由初始工具载荷指定的会话是否应进入手动锁定态。
 * NOTE: 若宿主显式指定查看某历史会话而此时另有运行中任务，锁定手动模式避免轮询抢回焦点。
 */
export function resolveInitialSelectionMode(
  snapshot: DashboardSnapshot | null,
  targetSessionId: string,
): SessionSelectionMode {
  const runningRoots = listRunningRootSessions(snapshot);
  if (runningRoots.length === 0) {
    return 'auto';
  }
  const target = findSessionSummaryById(snapshot, targetSessionId);
  if (target && target.status !== 'running') {
    return 'manual';
  }
  return 'auto';
}

/**
 * 在每次快照刷新后计算应当展示的会话 ID。
 * NOTE: 只要用户处于 manual 手动查看模式（如执行任务期间点击历史会话或子代理），
 * 绝不因为其他任务仍在运行而强制跳回，彻底解决执行过程中无法查看历史会话的问题。
 */
export function resolveNextSelectionOnSnapshot({
  snapshot,
  currentSessionId,
  selectionMode,
  autoFollow,
  searchActive,
}: SnapshotSelectionParams): string | null {
  const allRoots = snapshot.project_groups.flatMap((group) => group.sessions);
  const firstRoot = allRoots[0] ?? null;
  const primaryRunning = listRunningRootSessions(snapshot)[0] ?? null;

  if (!currentSessionId) {
    if (searchActive) {
      return firstRoot?.session_id ?? null;
    }
    return primaryRunning?.session_id ?? firstRoot?.session_id ?? null;
  }

  if (selectionMode === 'manual') {
    return currentSessionId;
  }

  if (!autoFollow || searchActive) {
    return currentSessionId;
  }

  const currentSummary = findSessionSummaryById(snapshot, currentSessionId);
  if (currentSummary?.status === 'running') {
    return currentSessionId;
  }

  return primaryRunning?.session_id ?? currentSessionId;
}

/**
 * 计算后台运行提示条信息：仅当用户当前查看的是非运行态会话、且后台仍有运行中任务时返回提示数据。
 */
export function resolveBackgroundRunningInfo(
  snapshot: DashboardSnapshot | null,
  selectedSessionId: string | null,
): BackgroundRunningInfo | null {
  const runningRoots = listRunningRootSessions(snapshot);
  if (runningRoots.length === 0) return null;

  const selectedSummary = findSessionSummaryById(snapshot, selectedSessionId);
  if (selectedSummary?.status === 'running') {
    return null;
  }

  const primarySession =
    runningRoots.find((session) => session.session_id !== selectedSessionId) ?? runningRoots[0];
  return {
    primarySession,
    runningCount: runningRoots.length,
  };
}

/**
 * 串行化会话详情请求、合并在途期间的新切换目标并维护有界内存缓存。
 * NOTE: 遵守 Antigravity MCP App 最多 4 个并发工具调用约束，同时确保切换会话时
 * 不会因旧轮询请求在途而静默丢弃用户的历史会话加载请求。
 */
export function createSessionDetailCoordinator({
  fetchDetail,
  getSelectedSessionId,
  onDetailLoaded,
  onLoadingChange,
  onError,
  maxCacheSize = 24,
}: DetailCoordinatorOptions): SessionDetailCoordinator {
  const cache = new Map<string, SessionDetail>();
  let inFlight = false;
  let disposed = false;
  let pending: { sessionId: string; showSpinner: boolean } | null = null;

  const setCacheEntry = (sessionId: string, detail: SessionDetail) => {
    cache.delete(sessionId);
    cache.set(sessionId, detail);
    if (cache.size > maxCacheSize) {
      const oldestKey = cache.keys().next().value;
      if (oldestKey !== undefined) {
        cache.delete(oldestKey);
      }
    }
  };

  const getCached = (sessionId: string | null): SessionDetail | null => {
    if (!sessionId) return null;
    const hit = cache.get(sessionId);
    if (!hit) return null;
    // 命中时刷新 LRU 顺序
    cache.delete(sessionId);
    cache.set(sessionId, hit);
    return hit;
  };

  const request = async (sessionId: string | null, showSpinner = false): Promise<void> => {
    if (disposed || !sessionId) return;

    if (inFlight) {
      const keepSpinner =
        showSpinner || (pending?.sessionId === sessionId && pending.showSpinner);
      pending = { sessionId, showSpinner: keepSpinner };
      if (keepSpinner && getSelectedSessionId() === sessionId) {
        onLoadingChange(true);
      }
      return;
    }

    inFlight = true;
    let next: { sessionId: string; showSpinner: boolean } | null = {
      sessionId,
      showSpinner,
    };

    try {
      while (next && !disposed) {
        const current = next;
        next = null;

        if (current.showSpinner && getSelectedSessionId() === current.sessionId) {
          onLoadingChange(true);
        }

        try {
          const detail = await fetchDetail(current.sessionId);
          if (disposed) return;
          setCacheEntry(current.sessionId, detail);
          if (getSelectedSessionId() === current.sessionId) {
            onDetailLoaded(current.sessionId, detail);
          }
        } catch (err: unknown) {
          if (disposed) return;
          if (getSelectedSessionId() === current.sessionId) {
            onError(
              current.sessionId,
              err instanceof Error ? err.message : String(err),
            );
          }
        }

        const queued = pending;
        pending = null;
        if (queued && queued.sessionId === getSelectedSessionId()) {
          next = queued;
        }
      }
    } finally {
      inFlight = false;
      if (!disposed) {
        onLoadingChange(false);
      }
    }
  };

  const dispose = () => {
    disposed = true;
    pending = null;
  };

  return {
    getCached,
    request,
    dispose,
  };
}
