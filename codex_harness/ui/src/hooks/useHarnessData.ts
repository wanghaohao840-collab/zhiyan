import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type {
  DashboardSnapshot,
  DispatchFormDraft,
  DispatchResponsePayload,
  HarnessViewMode,
  PacketPlanGroup,
  PlanDocSummary,
  SessionDetail,
  SpecInferenceResult,
  TaskPacketInfo,
} from '../types/harness';
import {
  createSessionDetailCoordinator,
  findSessionSummaryById,
  resolveBackgroundRunningInfo,
  resolveInitialSelectionMode,
  resolveNextSelectionOnSnapshot,
  type SessionDetailCoordinator,
  type SessionSelectionMode,
} from '../utils/sessionFollow';
import type { InitialToolPayload } from './useMcpApp';

export const EMPTY_DISPATCH_DRAFT: DispatchFormDraft = {
  title: '',
  objective: null,
  workspace: '',
  task_type: '',
  involves_ownership_check: null,
  involves_concurrency_or_lease: null,
  involves_cross_store_or_rollback: null,
  is_complex: null,
  plan_doc_path: '',
  packet_path: '',
  allowed_files_text: null,
  verification_text: null,
  auto_infer: true,
  resume_run_id: '',
  resume_codex_thread_id: '',
};

function stripFollowUpPrefix(raw: string): string {
  return raw.replace(/^(?:续轮派单\s*[:：]\s*)+/, '').trim();
}

function dispatchArguments(draft: DispatchFormDraft): Record<string, unknown> {
  const splitLines = (raw: string) => raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  return {
    title: stripFollowUpPrefix(draft.title),
    objective: draft.objective?.trim() || undefined,
    workspace: draft.workspace.trim() || undefined,
    task_type: draft.task_type || undefined,
    involves_ownership_check: draft.involves_ownership_check ?? undefined,
    involves_concurrency_or_lease: draft.involves_concurrency_or_lease ?? undefined,
    involves_cross_store_or_rollback: draft.involves_cross_store_or_rollback ?? undefined,
    is_complex: draft.is_complex ?? undefined,
    plan_doc_path: draft.plan_doc_path.trim() || undefined,
    packet_path: draft.packet_path.trim() || undefined,
    allowed_files: draft.allowed_files_text === null ? undefined : splitLines(draft.allowed_files_text),
    verification_commands: draft.verification_text?.trim() ? splitLines(draft.verification_text) : undefined,
    auto_infer: draft.auto_infer,
    resume_run_id: draft.resume_run_id.trim() || undefined,
    resume_codex_thread_id: draft.resume_codex_thread_id.trim() || undefined,
  };
}

interface SpecPreview {
  draft: DispatchFormDraft;
  revision: number;
  spec?: SpecInferenceResult;
  error?: string;
}

interface UseHarnessDataParams {
  connected: boolean;
  initialPayload: InitialToolPayload | null;
  callServerTool: <T = Record<string, unknown>>(
    name: string,
    args?: Record<string, unknown>,
  ) => Promise<T>;
}

/**
 * 负责监控快照轮询、自动跟随最新会话、会话详情拉取、任务包加载与显式派单提交。
 * NOTE: 严格控制并发请求数，避免触及 Antigravity MCP App 单应用最多 4 个在途调用限制。
 */
export function useHarnessData({
  connected,
  initialPayload,
  callServerTool,
}: UseHarnessDataParams) {
  const [viewMode, setViewMode] = useState<HarnessViewMode>('monitor');
  const [searchQuery, setSearchQuery] = useState('');
  const [liveSync, setLiveSync] = useState(true);
  const [autoFollow, setAutoFollow] = useState(true);

  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [loadedDetail, setLoadedDetail] = useState<{
    requestedSessionId: string;
    detail: SessionDetail;
  } | null>(null);
  const [packetPlans, setPacketPlans] = useState<PacketPlanGroup[]>([]);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const [dispatchDraft, setDispatchDraft] = useState<DispatchFormDraft>(EMPTY_DISPATCH_DRAFT);
  const [planDocs, setPlanDocs] = useState<PlanDocSummary[]>([]);
  const [preview, setPreview] = useState<SpecPreview | null>(null);
  const [specRevision, setSpecRevision] = useState(0);
  const [showGeneratedSpec, setShowGeneratedSpec] = useState(false);
  const [submittingDispatch, setSubmittingDispatch] = useState(false);
  const [lastDispatchResult, setLastDispatchResult] = useState<DispatchResponsePayload | null>(null);
  const [cancelRequest, setCancelRequest] = useState<{ runId: string; phase: 'sending' | 'pending' } | null>(null);

  const snapshotRef = useRef<DashboardSnapshot | null>(null);
  snapshotRef.current = snapshot;
  const snapshotInFlightRef = useRef(false);
  const pendingSnapshotQueryRef = useRef<string | null>(null);
  const specInFlightRef = useRef<Promise<SpecInferenceResult> | null>(null);
  const selectedIdRef = useRef<string | null>(null);
  const selectionModeRef = useRef<SessionSelectionMode>('auto');
  const autoFollowRef = useRef(autoFollow);
  autoFollowRef.current = autoFollow;
  const searchQueryRef = useRef(searchQuery);
  searchQueryRef.current = searchQuery;
  const detailCoordinatorRef = useRef<SessionDetailCoordinator | null>(null);

  useEffect(() => {
    const coordinator = createSessionDetailCoordinator({
      fetchDetail: (sessionId) =>
        callServerTool<SessionDetail>('app_get_session_detail', {
          session_id: sessionId,
        }),
      getSelectedSessionId: () => selectedIdRef.current,
      onDetailLoaded: (requestedSessionId, detail) => {
        setLoadedDetail({ requestedSessionId, detail });
        setErrorMessage(null);
      },
      onLoadingChange: setLoadingDetail,
      onError: (_sessionId, message) => {
        setErrorMessage(message);
      },
    });
    detailCoordinatorRef.current = coordinator;
    return () => {
      coordinator.dispose();
      if (detailCoordinatorRef.current === coordinator) {
        detailCoordinatorRef.current = null;
      }
    };
  }, [callServerTool]);

  // 响应工具推送的初始快照或新派单聚焦
  useEffect(() => {
    if (!initialPayload) return;
    if (initialPayload.view) {
      setViewMode(initialPayload.view);
    }
    const pushedSnapshot = initialPayload.snapshot as DashboardSnapshot | undefined;
    if (pushedSnapshot) {
      snapshotRef.current = pushedSnapshot;
      setSnapshot(pushedSnapshot);
    }
    const targetId = initialPayload.selected_session_id || initialPayload.session_id;
    if (targetId) {
      const nextMode = resolveInitialSelectionMode(
        pushedSnapshot ?? snapshotRef.current,
        targetId,
      );
      selectedIdRef.current = targetId;
      selectionModeRef.current = nextMode;
      setSelectedSessionId(targetId);
    }
  }, [initialPayload]);

  const refreshSnapshot = useCallback(
    async (queryOverride?: string) => {
      if (!connected) return;
      const requestedQuery = queryOverride !== undefined ? queryOverride : searchQueryRef.current;
      if (snapshotInFlightRef.current) {
        // 合入在途期间的最新查询，避免快速输入搜索词被静默丢弃
        pendingSnapshotQueryRef.current = requestedQuery;
        return;
      }

      snapshotInFlightRef.current = true;
      let nextQuery: string | null = requestedQuery;
      try {
        while (nextQuery !== null) {
          const currentQuery = nextQuery;
          nextQuery = null;
          const data = await callServerTool<DashboardSnapshot>('app_get_dashboard_snapshot', {
            query: currentQuery.trim() || undefined,
            limit: 80,
          });
          snapshotRef.current = data;
          setSnapshot(data);
          setErrorMessage(null);

          const nextSelectedId = resolveNextSelectionOnSnapshot({
            snapshot: data,
            currentSessionId: selectedIdRef.current,
            selectionMode: selectionModeRef.current,
            autoFollow: autoFollowRef.current,
            searchActive: Boolean(searchQueryRef.current.trim()),
          });
          if (nextSelectedId && nextSelectedId !== selectedIdRef.current) {
            selectedIdRef.current = nextSelectedId;
            setSelectedSessionId(nextSelectedId);
          }

          if (pendingSnapshotQueryRef.current !== null) {
            nextQuery = pendingSnapshotQueryRef.current;
            pendingSnapshotQueryRef.current = null;
          }
        }
      } catch (err: unknown) {
        setErrorMessage(err instanceof Error ? err.message : String(err));
      } finally {
        pendingSnapshotQueryRef.current = null;
        snapshotInFlightRef.current = false;
      }
    },
    [connected, callServerTool],
  );

  const refreshDetail = useCallback(
    async (sessionId: string | null, showSpinner = false) => {
      if (!connected || !sessionId || !detailCoordinatorRef.current) return;
      await detailCoordinatorRef.current.request(sessionId, showSpinner);
    },
    [connected],
  );

  const refreshTaskPackets = useCallback(async () => {
    if (!connected) return;
    try {
      const data = await callServerTool<{ plans: PacketPlanGroup[] }>('app_get_task_packets', {});
      setPacketPlans(data.plans ?? []);
    } catch (err: unknown) {
      setErrorMessage(err instanceof Error ? err.message : String(err));
    }
  }, [connected, callServerTool]);

  const refreshPlanDocs = useCallback(async () => {
    if (!connected) return;
    try {
      const data = await callServerTool<{ plans: PlanDocSummary[] }>('app_list_plan_docs', {});
      setPlanDocs(data.plans ?? []);
    } catch {
      // 方案列表非关键路径，静默忽略
    }
  }, [connected, callServerTool]);

  const changeDispatchDraft = useCallback((draft: DispatchFormDraft) => {
    setDispatchDraft(draft);
    setLastDispatchResult(null);
  }, []);

  const inferDispatchSpec = useCallback(() => {
    setDispatchDraft((draft) => draft.auto_infer ? draft : { ...draft, auto_infer: true });
    setShowGeneratedSpec(true);
    setSpecRevision((revision) => revision + 1);
    setLastDispatchResult(null);
  }, []);

  const resetDispatchDraft = useCallback(() => {
    changeDispatchDraft(EMPTY_DISPATCH_DRAFT);
    setShowGeneratedSpec(false);
  }, [changeDispatchDraft]);

  // 初次连接拉取快照
  useEffect(() => {
    if (!connected) return;
    void refreshSnapshot();
  }, [connected, refreshSnapshot]);

  // 选中会话切换时立即拉取详情（命中缓存时静默刷新，未命中时显示加载态）
  useEffect(() => {
    if (!connected || !selectedSessionId) return;
    const hasCached = Boolean(detailCoordinatorRef.current?.getCached(selectedSessionId));
    void refreshDetail(selectedSessionId, !hasCached);
  }, [connected, selectedSessionId, refreshDetail]);

  // 切换到任务包或派单视图时按需加载对应仓库清单
  useEffect(() => {
    if (!connected) return;
    if (viewMode === 'packets') {
      void refreshTaskPackets();
    } else if (viewMode === 'dispatch') {
      void refreshPlanDocs();
    }
  }, [connected, viewMode, refreshTaskPackets, refreshPlanDocs]);

  // 实时同步轮询（默认 2.8s，仅在 liveSync 开启时运行）
  useEffect(() => {
    if (!connected || !liveSync) return;
    const timer = window.setInterval(() => {
      void refreshSnapshot();
      if (viewMode === 'monitor' && selectedIdRef.current) {
        void refreshDetail(selectedIdRef.current, false);
      }
    }, 2800);
    return () => window.clearInterval(timer);
  }, [connected, liveSync, viewMode, refreshSnapshot, refreshDetail]);

  // 同一份表单参数也用于干跑与提交，及时丢弃已经过期的预览响应。
  useEffect(() => {
    if (!connected || viewMode !== 'dispatch') return;
    let active = true;
    async function loadPreview() {
      await specInFlightRef.current?.catch(() => undefined);
      if (!active) return;
      const request = callServerTool<SpecInferenceResult>('app_preview_dispatch', dispatchArguments(dispatchDraft));
      specInFlightRef.current = request;
      try {
        const spec = await request;
        if (active) setPreview({ draft: dispatchDraft, revision: specRevision, spec });
      } catch (err: unknown) {
        const error = err instanceof Error ? err.message : String(err);
        if (active) setPreview({ draft: dispatchDraft, revision: specRevision, error });
      } finally {
        if (specInFlightRef.current === request) specInFlightRef.current = null;
      }
    }
    const timer = window.setTimeout(() => void loadPreview(), 150);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [connected, viewMode, dispatchDraft, specRevision, callServerTool]);

  const currentPreview = preview?.draft === dispatchDraft && preview.revision === specRevision ? preview : null;
  const showSpec = showGeneratedSpec || Boolean(
    dispatchDraft.plan_doc_path || dispatchDraft.packet_path || dispatchDraft.resume_run_id || dispatchDraft.resume_codex_thread_id,
  );

  const applySelectedSession = useCallback(
    (sessionId: string, mode: SessionSelectionMode) => {
      const prevId = selectedIdRef.current;
      selectedIdRef.current = sessionId;
      selectionModeRef.current = mode;
      setSelectedSessionId(sessionId);
      setViewMode('monitor');
      setErrorMessage(null);

      const cached = detailCoordinatorRef.current?.getCached(sessionId) ?? null;
      if (cached) {
        setLoadedDetail({ requestedSessionId: sessionId, detail: cached });
        setLoadingDetail(false);
      } else if (prevId !== sessionId) {
        // 切换至未缓存会话时立即清空旧详情，避免右侧残留正在运行任务的内容
        setLoadedDetail(null);
        setLoadingDetail(true);
      }

      if (prevId === sessionId) {
        void refreshDetail(sessionId, !cached);
      }
    },
    [refreshDetail],
  );

  const selectSession = useCallback(
    (sessionId: string) => {
      // 用户主动点击会话（包括历史会话、子代理或其它运行中会话）时锁定手动查看态
      applySelectedSession(sessionId, 'manual');
    },
    [applySelectedSession],
  );

  const returnToRunningSession = useCallback(
    (sessionId: string) => {
      autoFollowRef.current = true;
      setAutoFollow(true);
      applySelectedSession(sessionId, 'auto');
    },
    [applySelectedSession],
  );

  const updateSearchQuery = useCallback(
    (nextQuery: string) => {
      searchQueryRef.current = nextQuery;
      setSearchQuery(nextQuery);
      void refreshSnapshot(nextQuery);
    },
    [refreshSnapshot],
  );

  const toggleAutoFollow = useCallback(() => {
    setAutoFollow((prev) => {
      const next = !prev;
      autoFollowRef.current = next;
      return next;
    });
  }, []);

  const prepareDispatchFromPacket = useCallback(
    (packet: TaskPacketInfo) => {
      setShowGeneratedSpec(false);
      changeDispatchDraft({
        ...EMPTY_DISPATCH_DRAFT,
        title: `实现任务包 ${packet.packet_id}: ${packet.title}`,
        packet_path: packet.relative_path,
      });
      setViewMode('dispatch');
    },
    [changeDispatchDraft],
  );

  const prepareFollowUpDispatch = useCallback(
    (detail: SessionDetail) => {
      setShowGeneratedSpec(false);
      const s = detail.summary;
      changeDispatchDraft({
        ...EMPTY_DISPATCH_DRAFT,
        title: stripFollowUpPrefix(s.title),
        workspace: s.workspace,
        packet_path: s.packet_path ?? '',
        resume_run_id: s.run_id ?? '',
        resume_codex_thread_id: s.codex_thread_id ?? '',
      });
      setViewMode('dispatch');
    },
    [changeDispatchDraft],
  );

  const submitDispatch = useCallback(
    async (dryRun: boolean) => {
      const cleanTitle = stripFollowUpPrefix(dispatchDraft.title);
      if (
        !cleanTitle &&
        !dispatchDraft.plan_doc_path.trim() &&
        !dispatchDraft.packet_path.trim() &&
        !dispatchDraft.resume_run_id &&
        !dispatchDraft.resume_codex_thread_id
      ) {
        setErrorMessage('请至少填写派单标题或选择一份仓库方案文档');
        return;
      }
      if (!dispatchDraft.auto_infer && !dispatchDraft.objective?.trim()) {
        setErrorMessage('未启用自动补全时，请填写目标与可验证完成条件');
        return;
      }
      setSubmittingDispatch(true);
      setErrorMessage(null);
      try {
        const res = await callServerTool<DispatchResponsePayload>('app_create_dispatch', {
          ...dispatchArguments(dispatchDraft),
          dry_run: dryRun,
        });
        setLastDispatchResult(res);
        if (!dryRun) {
          applySelectedSession(res.session_id, 'auto');
          await refreshSnapshot();
          await refreshDetail(res.session_id, true);
        }
      } catch (err: unknown) {
        setErrorMessage(err instanceof Error ? err.message : String(err));
      } finally {
        setSubmittingDispatch(false);
      }
    },
    [dispatchDraft, callServerTool, applySelectedSession, refreshSnapshot, refreshDetail],
  );

  const cancelRun = useCallback(
    async (runId: string) => {
      setCancelRequest({ runId, phase: 'sending' });
      try {
        const result = await callServerTool<{ cancel_requested: boolean }>('app_cancel_dispatch', { run_id: runId });
        setCancelRequest((current) => current?.runId !== runId ? current : (
          result.cancel_requested ? { runId, phase: 'pending' } : null
        ));
        await refreshSnapshot();
        if (selectedIdRef.current) {
          await refreshDetail(selectedIdRef.current, false);
        }
      } catch (err: unknown) {
        setCancelRequest((current) => current?.runId === runId ? null : current);
        setErrorMessage(err instanceof Error ? err.message : String(err));
      }
    },
    [callServerTool, refreshSnapshot, refreshDetail],
  );

  const sessionDetail =
    loadedDetail && loadedDetail.requestedSessionId === selectedSessionId
      ? loadedDetail.detail
      : null;

  const selectedSummary = useMemo(
    () => findSessionSummaryById(snapshot, selectedSessionId),
    [snapshot, selectedSessionId],
  );

  const backgroundRunning = useMemo(
    () => resolveBackgroundRunningInfo(snapshot, selectedSessionId),
    [snapshot, selectedSessionId],
  );

  return {
    viewMode,
    setViewMode,
    searchQuery,
    setSearchQuery,
    updateSearchQuery,
    liveSync,
    setLiveSync,
    autoFollow,
    setAutoFollow,
    toggleAutoFollow,
    snapshot,
    selectedSessionId,
    selectedSummary,
    backgroundRunning,
    selectSession,
    returnToRunningSession,
    sessionDetail,
    loadingDetail,
    packetPlans,
    errorMessage,
    setErrorMessage,
    dispatchDraft,
    changeDispatchDraft,
    resetDispatchDraft,
    planDocs,
    routingPreview: currentPreview?.spec?.routing_preview ?? null,
    inferredSpec: showSpec ? currentPreview?.spec ?? null : null,
    specError: currentPreview?.error,
    inferringSpec: connected && currentPreview === null,
    inferDispatchSpec,
    submittingDispatch,
    lastDispatchResult,
    setLastDispatchResult,
    refreshSnapshot,
    refreshDetail,
    refreshTaskPackets,
    prepareDispatchFromPacket,
    prepareFollowUpDispatch,
    submitDispatch,
    cancelRun,
    cancelRequest,
  };
}
