import { X } from 'lucide-react';
import { DispatchView } from './components/DispatchView';
import { SessionDetail } from './components/SessionDetail';
import { SidebarTree } from './components/SidebarTree';
import { TaskPacketsView } from './components/TaskPacketsView';
import { useHarnessData } from './hooks/useHarnessData';
import { useMcpApp } from './hooks/useMcpApp';

/**
 * Antigravity × Codex Monitor MCP App 根组件
 */
export function App() {
  const {
    connected,
    initialPayload,
    connectionError,
    callServerTool,
    openWorkspaceFile,
  } = useMcpApp();

  const harness = useHarnessData({
    connected,
    initialPayload,
    callServerTool,
  });

  if (connectionError) {
    return (
      <div className="ch-empty">
        <strong>MCP App 宿主握手失败</strong>
        <span>{connectionError}</span>
      </div>
    );
  }

  return (
    <div className="ch-shell">
      <SidebarTree
        viewMode={harness.viewMode}
        onChangeView={harness.setViewMode}
        searchQuery={harness.searchQuery}
        onChangeSearch={(q) => {
          harness.setSearchQuery(q);
          void harness.refreshSnapshot(q);
        }}
        groups={harness.snapshot?.project_groups ?? []}
        selectedSessionId={harness.selectedSessionId}
        onSelectSession={harness.selectSession}
        runningCount={harness.snapshot?.running_dispatches ?? 0}
      />

      <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0, height: '100%' }}>
        {harness.errorMessage && (
          <div className="ch-banner-err" role="alert">
            <span>{harness.errorMessage}</span>
            <button
              type="button"
              className="ch-btn ch-btn-xs"
              onClick={() => harness.setErrorMessage(null)}
              aria-label="关闭提示"
            >
              <X size={12} />
            </button>
          </div>
        )}

        {harness.viewMode === 'monitor' && (
          <SessionDetail
            detail={harness.sessionDetail}
            loading={harness.loadingDetail || !connected}
            liveSync={harness.liveSync}
            onToggleLiveSync={() => harness.setLiveSync((v) => !v)}
            autoFollow={harness.autoFollow}
            onToggleAutoFollow={() => harness.setAutoFollow((v) => !v)}
            onManualRefresh={() => {
              void harness.refreshSnapshot();
              if (harness.selectedSessionId) {
                void harness.refreshDetail(harness.selectedSessionId, false);
              }
            }}
            onOpenFile={(absPath) => {
              void openWorkspaceFile(absPath);
            }}
            onSelectSession={harness.selectSession}
            onNewDispatch={() => {
              harness.resetDispatchDraft();
              harness.setViewMode('dispatch');
            }}
            onFollowUpDispatch={harness.prepareFollowUpDispatch}
            onCancelRun={(runId) => {
              void harness.cancelRun(runId);
            }}
            cancelPhase={harness.cancelRequest?.runId === harness.sessionDetail?.summary.run_id
              ? harness.cancelRequest?.phase
              : undefined}
            delegationDocPath={harness.snapshot?.delegation_doc_path}
          />
        )}

        {harness.viewMode === 'packets' && (
          <TaskPacketsView
            plans={harness.packetPlans}
            onRefresh={() => {
              void harness.refreshTaskPackets();
            }}
            onOpenFile={(absPath) => {
              void openWorkspaceFile(absPath);
            }}
            onDispatchPacket={harness.prepareDispatchFromPacket}
          />
        )}

        {harness.viewMode === 'dispatch' && (
          <DispatchView
            draft={harness.dispatchDraft}
            onChangeDraft={harness.changeDispatchDraft}
            planDocs={harness.planDocs}
            routingPreview={harness.routingPreview}
            inferredSpec={harness.inferredSpec}
            specError={harness.specError}
            inferring={harness.inferringSpec}
            onInferSpec={harness.inferDispatchSpec}
            onResetDraft={harness.resetDispatchDraft}
            submitting={harness.submittingDispatch}
            lastResult={harness.lastDispatchResult}
            onSubmit={(dryRun) => {
              void harness.submitDispatch(dryRun);
            }}
            codexAvailable={harness.snapshot?.codex_cli_available ?? true}
          />
        )}
      </div>
    </div>
  );
}

export default App;
