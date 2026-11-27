import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  DashboardSnapshot,
  DispatchFormDraft,
  DispatchResponsePayload,
  HarnessViewMode,
  PacketPlanGroup,
  RoutingDecision,
  SessionDetail,
  TaskPacketInfo,
} from '../types/harness';
import type { InitialToolPayload } from './useMcpApp';

export const EMPTY_DISPATCH_DRAFT: DispatchFormDraft = {
  title: '',
  objective: '',
  task_type: 'implement_general',
  involves_ownership_check: false,
  involves_concurrency_or_lease: false,
  involves_cross_store_or_rollback: false,
  is_complex: false,
  packet_path: '',
  allowed_files_text: '',
  forbidden_paths_text: '',
  acceptance_text: '',
  verification_text: '',
  resume_run_id: '',
  resume_codex_thread_id: '',
};

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
  const [sessionDetail, setSessionDetail] = useState<SessionDetail | null>(null);
  const [packetPlans, setPacketPlans] = useState<PacketPlanGroup[]>([]);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const [dispatchDraft, setDispatchDraft] = useState<DispatchFormDraft>(EMPTY_DISPATCH_DRAFT);
  const [routingPreview, setRoutingPreview] = useState<RoutingDecision | null>(null);
  const [submittingDispatch, setSubmittingDispatch] = useState(false);
  const [lastDispatchResult, setLastDispatchResult] = useState<DispatchResponsePayload | null>(null);

  const snapshotInFlightRef = useRef(false);
  const detailInFlightRef = useRef(false);
  const selectedIdRef = useRef<string | null>(null);
  selectedIdRef.current = selectedSessionId;

  // 响应工具推送的初始快照或新派单聚焦
  useEffect(() => {
    if (!initialPayload) return;
    if (initialPayload.view) {
      setViewMode(initialPayload.view);
    }
    if (initialPayload.snapshot) {
      setSnapshot(initialPayload.snapshot as DashboardSnapshot);
    }
    const targetId = initialPayload.selected_session_id || initialPayload.session_id;
    if (targetId) {
      setSelectedSessionId(targetId);
    }
  }, [initialPayload]);

  const refreshSnapshot = useCallback(
    async (queryOverride?: string) => {
      if (!connected || snapshotInFlightRef.current) return;
      snapshotInFlightRef.current = true;
      try {
        const q = queryOverride !== undefined ? queryOverride : searchQuery;
        const data = await callServerTool<DashboardSnapshot>('app_get_dashboard_snapshot', {
          query: q.trim() || undefined,
          limit: 80,
        });
        setSnapshot(data);
        setErrorMessage(null);

        const firstSession = data.project_groups[0]?.sessions[0];
        if (!selectedIdRef.current && firstSession) {
          setSelectedSessionId(firstSession.session_id);
        } else if (autoFollow && firstSession) {
          // 自动跟随：当出现最新活跃/运行中会话且当前不在阅读历史旧会话时平滑切至最新
          const runningSession = data.project_groups
            .flatMap((g) => g.sessions)
            .find((s) => s.status === 'running');
          if (runningSession && runningSession.session_id !== selectedIdRef.current) {
            setSelectedSessionId(runningSession.session_id);
          }
        }
      } catch (err: unknown) {
        setErrorMessage(err instanceof Error ? err.message : String(err));
      } finally {
        snapshotInFlightRef.current = false;
      }
    },
    [connected, searchQuery, autoFollow, callServerTool],
  );

  const refreshDetail = useCallback(
    async (sessionId: string | null, showSpinner = false) => {
      if (!connected || !sessionId || detailInFlightRef.current) return;
      detailInFlightRef.current = true;
      if (showSpinner) setLoadingDetail(true);
      try {
        const detail = await callServerTool<SessionDetail>('app_get_session_detail', {
          session_id: sessionId,
        });
        // 防止切走后旧请求回写覆盖新选中项
        if (selectedIdRef.current === sessionId) {
          setSessionDetail(detail);
        }
      } catch (err: unknown) {
        if (selectedIdRef.current === sessionId) {
          setErrorMessage(err instanceof Error ? err.message : String(err));
        }
      } finally {
        detailInFlightRef.current = false;
        if (showSpinner) setLoadingDetail(false);
      }
    },
    [connected, callServerTool],
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

  // 初次连接拉取快照
  useEffect(() => {
    if (!connected) return;
    void refreshSnapshot();
  }, [connected, refreshSnapshot]);

  // 选中会话切换时立即拉取详情
  useEffect(() => {
    if (!connected || !selectedSessionId) return;
    void refreshDetail(selectedSessionId, true);
  }, [connected, selectedSessionId, refreshDetail]);

  // 切换到任务包视图时刷新任务包树
  useEffect(() => {
    if (connected && viewMode === 'packets') {
      void refreshTaskPackets();
    }
  }, [connected, viewMode, refreshTaskPackets]);

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

  // 派单表单实时预览 AGENTS.md 路由决策
  useEffect(() => {
    if (!connected || viewMode !== 'dispatch') return;
    let active = true;
    const timer = window.setTimeout(() => {
      void callServerTool<RoutingDecision>('app_preview_routing', {
        task_type: dispatchDraft.task_type,
        involves_ownership_check: dispatchDraft.involves_ownership_check,
        involves_concurrency_or_lease: dispatchDraft.involves_concurrency_or_lease,
        involves_cross_store_or_rollback: dispatchDraft.involves_cross_store_or_rollback,
        is_complex: dispatchDraft.is_complex,
      })
        .then((res) => {
          if (active) setRoutingPreview(res);
        })
        .catch(() => {
          // 预览失败不影响主界面交互
        });
    }, 150);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [
    connected,
    viewMode,
    dispatchDraft.task_type,
    dispatchDraft.involves_ownership_check,
    dispatchDraft.involves_concurrency_or_lease,
    dispatchDraft.involves_cross_store_or_rollback,
    dispatchDraft.is_complex,
    callServerTool,
  ]);

  const selectSession = useCallback((sessionId: string) => {
    setSelectedSessionId(sessionId);
    setViewMode('monitor');
  }, []);

  const prepareDispatchFromPacket = useCallback((packet: TaskPacketInfo) => {
    setLastDispatchResult(null);
    setDispatchDraft({
      ...EMPTY_DISPATCH_DRAFT,
      title: `实现任务包 ${packet.packet_id}: ${packet.title}`,
      objective:
        packet.goal_excerpt ||
        `请严格按任务包 ${packet.relative_path} 完成实现与验收，不要改动白名单之外的文件。`,
      task_type: 'implement_general',
      packet_path: packet.relative_path,
      allowed_files_text: packet.allowed_files.join('\n'),
      verification_text: packet.verification_commands.join('\n'),
    });
    setViewMode('dispatch');
  }, []);

  const prepareFollowUpDispatch = useCallback((detail: SessionDetail) => {
    setLastDispatchResult(null);
    const s = detail.summary;
    setDispatchDraft({
      ...EMPTY_DISPATCH_DRAFT,
      title: `续轮派单：${s.title}`,
      objective: '',
      task_type: s.role === 'astra' ? 'review_concurrency' : 'implement_general',
      resume_run_id: s.run_id ?? '',
      resume_codex_thread_id: s.codex_thread_id ?? '',
    });
    setViewMode('dispatch');
  }, []);

  const submitDispatch = useCallback(
    async (dryRun: boolean) => {
      if (!dispatchDraft.title.trim() || !dispatchDraft.objective.trim()) {
        setErrorMessage('请填写派单标题与任务目标');
        return;
      }
      setSubmittingDispatch(true);
      setErrorMessage(null);
      try {
        const splitLines = (raw: string) =>
          raw
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter(Boolean);

        const res = await callServerTool<DispatchResponsePayload>('app_create_dispatch', {
          title: dispatchDraft.title.trim(),
          objective: dispatchDraft.objective.trim(),
          task_type: dispatchDraft.task_type,
          involves_ownership_check: dispatchDraft.involves_ownership_check,
          involves_concurrency_or_lease: dispatchDraft.involves_concurrency_or_lease,
          involves_cross_store_or_rollback: dispatchDraft.involves_cross_store_or_rollback,
          is_complex: dispatchDraft.is_complex,
          packet_path: dispatchDraft.packet_path.trim() || undefined,
          allowed_files: splitLines(dispatchDraft.allowed_files_text),
          forbidden_paths: splitLines(dispatchDraft.forbidden_paths_text),
          acceptance_criteria: splitLines(dispatchDraft.acceptance_text),
          verification_commands: splitLines(dispatchDraft.verification_text),
          resume_run_id: dispatchDraft.resume_run_id.trim() || undefined,
          resume_codex_thread_id: dispatchDraft.resume_codex_thread_id.trim() || undefined,
          dry_run: dryRun,
        });
        setLastDispatchResult(res);
        if (!dryRun) {
          setSelectedSessionId(res.session_id);
          setViewMode('monitor');
          await refreshSnapshot();
          await refreshDetail(res.session_id, true);
        }
      } catch (err: unknown) {
        setErrorMessage(err instanceof Error ? err.message : String(err));
      } finally {
        setSubmittingDispatch(false);
      }
    },
    [dispatchDraft, callServerTool, refreshSnapshot, refreshDetail],
  );

  const cancelRun = useCallback(
    async (runId: string) => {
      try {
        await callServerTool('app_cancel_dispatch', { run_id: runId });
        await refreshSnapshot();
        if (selectedIdRef.current) {
          await refreshDetail(selectedIdRef.current, false);
        }
      } catch (err: unknown) {
        setErrorMessage(err instanceof Error ? err.message : String(err));
      }
    },
    [callServerTool, refreshSnapshot, refreshDetail],
  );

  return {
    viewMode,
    setViewMode,
    searchQuery,
    setSearchQuery,
    liveSync,
    setLiveSync,
    autoFollow,
    setAutoFollow,
    snapshot,
    selectedSessionId,
    selectSession,
    sessionDetail,
    loadingDetail,
    packetPlans,
    errorMessage,
    setErrorMessage,
    dispatchDraft,
    setDispatchDraft,
    routingPreview,
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
  };
}
