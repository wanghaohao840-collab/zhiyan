import assert from 'node:assert/strict';
import test from 'node:test';
import type { DashboardSnapshot, SessionDetail, SessionSummary } from '../src/types/harness.ts';
import {
  createSessionDetailCoordinator,
  findSessionSummaryById,
  resolveBackgroundRunningInfo,
  resolveInitialSelectionMode,
  resolveNextSelectionOnSnapshot,
} from '../src/utils/sessionFollow.ts';

function makeSummary(overrides: Partial<SessionSummary>): SessionSummary {
  return {
    session_id: 'session-default',
    source_kind: 'codex_session',
    project_name: 'python_self_agent',
    workspace: 'D:\\python_self_agent',
    title: '默认会话',
    model: 'gpt-6.2-sol',
    effort: 'high',
    turn_count: 1,
    status: 'completed',
    updated_ts: 1000,
    latest_activity_ts: 1000,
    is_subagent: false,
    command_count: 0,
    file_count: 0,
    scope_status: 'unchecked',
    child_sessions: [],
    ...overrides,
  };
}

function makeSnapshot(sessions: SessionSummary[]): DashboardSnapshot {
  return {
    workspace: 'D:\\python_self_agent',
    codex_cli_available: true,
    total_sessions: sessions.length,
    running_dispatches: sessions.filter((s) => s.status === 'running').length,
    harness_runs: sessions.filter((s) => s.source_kind === 'harness').length,
    project_groups: [
      {
        project_name: 'python_self_agent',
        workspace: 'D:\\python_self_agent',
        session_count: sessions.length,
        running_count: sessions.filter((s) => s.status === 'running').length,
        latest_updated_ts: Math.max(0, ...sessions.map((s) => s.latest_activity_ts ?? s.updated_ts)),
        sessions,
      },
    ],
    packet_plans_summary: [],
    server_time: '2026-11-28T14:00:00Z',
  };
}

function makeDetail(summary: SessionSummary): SessionDetail {
  return {
    summary,
    turns: [],
  };
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (err: unknown) => void;
}

function createDeferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

test('执行会话1期间点击历史会话2，快照轮询不会抢回会话1，且返回后台运行提示', () => {
  const session1 = makeSummary({
    session_id: 'session-1',
    title: 'Agent 工作流与可靠性',
    status: 'running',
    updated_ts: 2000,
    latest_activity_ts: 2000,
  });
  const session2 = makeSummary({
    session_id: 'session-2',
    source_kind: 'harness',
    title: '一、逐项核对总览（共 13 项）',
    status: 'failed',
    updated_ts: 1500,
    latest_activity_ts: 1500,
  });
  const snapshot = makeSnapshot([session1, session2]);

  // 初次进入无选中项时优先定位到运行中的会话1
  assert.equal(
    resolveNextSelectionOnSnapshot({
      snapshot,
      currentSessionId: null,
      selectionMode: 'auto',
      autoFollow: true,
      searchActive: false,
    }),
    'session-1',
  );

  // 用户手动点击历史会话2进入 manual 模式，即使 autoFollow=true 也不得跳回会话1
  assert.equal(
    resolveNextSelectionOnSnapshot({
      snapshot,
      currentSessionId: 'session-2',
      selectionMode: 'manual',
      autoFollow: true,
      searchActive: false,
    }),
    'session-2',
  );

  // 此时应提示后台仍有会话1在运行，支持一键回到运行中
  const bgInfo = resolveBackgroundRunningInfo(snapshot, 'session-2');
  assert.ok(bgInfo);
  assert.equal(bgInfo.primarySession.session_id, 'session-1');
  assert.equal(bgInfo.runningCount, 1);

  // 切回运行中会话1后不再显示后台运行提示条
  assert.equal(resolveBackgroundRunningInfo(snapshot, 'session-1'), null);
});

test('查看运行中主会话的历史子代理时不会被拉回主会话，而查看运行中子代理不弹提示条', () => {
  const childDone = makeSummary({
    session_id: 'child-done',
    title: '[Einstein] 已完成审查',
    status: 'completed',
    is_subagent: true,
    parent_thread_id: 'session-1',
    updated_ts: 1800,
    latest_activity_ts: 1800,
  });
  const childRunning = makeSummary({
    session_id: 'child-running',
    title: '[Poincare] 正在检查',
    status: 'running',
    is_subagent: true,
    parent_thread_id: 'session-1',
    updated_ts: 2100,
    latest_activity_ts: 2100,
  });
  const session1 = makeSummary({
    session_id: 'session-1',
    title: 'Agent 工作流与可靠性',
    status: 'running',
    updated_ts: 2100,
    latest_activity_ts: 2100,
    child_sessions: [childRunning, childDone],
  });
  const snapshot = makeSnapshot([session1]);

  assert.equal(findSessionSummaryById(snapshot, 'child-done')?.session_id, 'child-done');
  assert.equal(
    resolveNextSelectionOnSnapshot({
      snapshot,
      currentSessionId: 'child-done',
      selectionMode: 'manual',
      autoFollow: true,
      searchActive: false,
    }),
    'child-done',
  );

  // 查看已完成子代理时提示主会话仍在运行；查看运行中子代理时不显示提示条
  assert.equal(resolveBackgroundRunningInfo(snapshot, 'child-done')?.primarySession.session_id, 'session-1');
  assert.equal(resolveBackgroundRunningInfo(snapshot, 'child-running'), null);
});

test('空闲自动模式下出现新运行任务可平滑跟随，但关闭自动跟随或宿主显式打开历史会话时不抢焦点', () => {
  const session1 = makeSummary({ session_id: 'session-1', status: 'running', updated_ts: 2000, latest_activity_ts: 2000 });
  const session2 = makeSummary({ session_id: 'session-2', status: 'completed', updated_ts: 1500, latest_activity_ts: 1500 });
  const snapshot = makeSnapshot([session1, session2]);

  assert.equal(
    resolveNextSelectionOnSnapshot({
      snapshot,
      currentSessionId: 'session-2',
      selectionMode: 'auto',
      autoFollow: true,
      searchActive: false,
    }),
    'session-1',
  );

  assert.equal(
    resolveNextSelectionOnSnapshot({
      snapshot,
      currentSessionId: 'session-2',
      selectionMode: 'auto',
      autoFollow: false,
      searchActive: false,
    }),
    'session-2',
  );

  // 宿主通过 open_codex_harness(session_id="session-2") 显式打开历史会话时应锁定 manual
  assert.equal(resolveInitialSelectionMode(snapshot, 'session-2'), 'manual');
  assert.equal(resolveInitialSelectionMode(snapshot, 'session-1'), 'auto');
});

test('详情协调器在会话1请求在途时不会丢弃会话2切换请求，且合并连续快速点击', async () => {
  const s1 = makeSummary({ session_id: 'session-1', status: 'running', title: '运行中1' });
  const s2 = makeSummary({ session_id: 'session-2', status: 'completed', title: '历史2' });
  const s3 = makeSummary({ session_id: 'session-3', status: 'completed', title: '历史3' });

  const deferredById = new Map<string, Deferred<SessionDetail>>();
  const fetchedOrder: string[] = [];
  const loadedHistory: string[] = [];
  const loadingStates: boolean[] = [];
  let selectedId: string | null = 'session-1';

  const coordinator = createSessionDetailCoordinator({
    fetchDetail: (sessionId) => {
      fetchedOrder.push(sessionId);
      const d = createDeferred<SessionDetail>();
      deferredById.set(sessionId, d);
      return d.promise;
    },
    getSelectedSessionId: () => selectedId,
    onDetailLoaded: (requestedId) => {
      loadedHistory.push(requestedId);
    },
    onLoadingChange: (loading) => {
      loadingStates.push(loading);
    },
    onError: (_id, msg) => {
      assert.fail(`Unexpected error: ${msg}`);
    },
  });

  // 1. 后台轮询开始拉取 session-1，此时尚未返回
  const firstPoll = coordinator.request('session-1', false);
  assert.deepEqual(fetchedOrder, ['session-1']);

  // 2. 在 session-1 在途期间，用户先点 session-2，又快速点 session-3
  selectedId = 'session-2';
  await coordinator.request('session-2', true);
  selectedId = 'session-3';
  await coordinator.request('session-3', true);

  // 并发始终控制在 1 个在途详情请求
  assert.deepEqual(fetchedOrder, ['session-1']);

  // 3. session-1 返回：只入缓存，不覆盖当前选中的 session-3，并立即拉取最新的 session-3（跳过 session-2）
  deferredById.get('session-1')!.resolve(makeDetail(s1));
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.deepEqual(loadedHistory, []);
  assert.deepEqual(fetchedOrder, ['session-1', 'session-3']);
  assert.equal(coordinator.getCached('session-1')?.summary.title, '运行中1');

  // 4. session-3 返回：正确展示并结束加载态
  deferredById.get('session-3')!.resolve(makeDetail(s3));
  await firstPoll;

  assert.deepEqual(loadedHistory, ['session-3']);
  assert.equal(coordinator.getCached('session-3')?.summary.title, '历史3');
  assert.equal(loadingStates[loadingStates.length - 1], false);

  // 5. 再切换到 session-2，立刻可拉取并缓存
  selectedId = 'session-2';
  const fetchS2 = coordinator.request('session-2', true);
  deferredById.get('session-2')!.resolve(makeDetail(s2));
  await fetchS2;
  assert.deepEqual(loadedHistory, ['session-3', 'session-2']);
});
