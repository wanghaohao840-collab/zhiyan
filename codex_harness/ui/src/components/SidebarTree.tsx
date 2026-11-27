import { useMemo, useState } from 'react';
import {
  Activity,
  ChevronDown,
  ChevronRight,
  Clock,
  Folder,
  FolderOpen,
  GitBranch,
  Layers,
  Search,
  Send,
} from 'lucide-react';
import type {
  HarnessViewMode,
  ProjectSessionGroup,
  SessionSummary,
} from '../types/harness';
import { formatShortTimestamp } from '../utils/format';

type SidebarFilterMode = 'project' | 'timeline' | 'harness';

interface SidebarTreeProps {
  viewMode: HarnessViewMode;
  onChangeView: (view: HarnessViewMode) => void;
  searchQuery: string;
  onChangeSearch: (query: string) => void;
  groups: ProjectSessionGroup[];
  selectedSessionId: string | null;
  onSelectSession: (sessionId: string) => void;
  runningCount: number;
}

/**
 * Antigravity × Codex 双标徽章（纯内联 SVG，兼容默认 CSP）
 */
function BrandGlyphs() {
  return (
    <span className="ch-brand-icons" aria-hidden="true">
      <span className="ch-icon-badge" title="Antigravity">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
          <path
            d="M12 3L4.5 20.5h3.7l1.8-4.4h4l1.8 4.4h3.7L12 3zm0 5.6l1.3 4.3h-2.6L12 8.6z"
            fill="#4f46e5"
          />
        </svg>
      </span>
      <span className="ch-icon-badge" title="Codex">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="8.2" stroke="currentColor" strokeWidth="1.8" />
          <path
            d="M9.2 9.5L6.8 12l2.4 2.5M14.8 9.5l2.4 2.5-2.4 2.5M12.9 8.2l-1.8 7.6"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </span>
    </span>
  );
}

/**
 * 按当前本地自然日计算时间分组标签（今天 / 昨天 / 近 7 天 / 更早）
 */
function getTimeBucketLabel(tsSeconds: number): string {
  if (!tsSeconds) return '更早';
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  const daySec = 86400;
  if (tsSeconds >= startOfToday) return '今天';
  if (tsSeconds >= startOfToday - daySec) return '昨天';
  if (tsSeconds >= startOfToday - 6 * daySec) return '近 7 天';
  return '更早';
}

const BUCKET_ORDER = ['今天', '昨天', '近 7 天', '更早'];

function groupByTimeBuckets(sessions: SessionSummary[]): Array<{ label: string; items: SessionSummary[] }> {
  const buckets = new Map<string, SessionSummary[]>();
  for (const s of sessions) {
    const ts = s.latest_activity_ts || s.updated_ts;
    const label = getTimeBucketLabel(ts);
    const list = buckets.get(label) ?? [];
    list.push(s);
    buckets.set(label, list);
  }
  return BUCKET_ORDER.filter((label) => buckets.has(label)).map((label) => ({
    label,
    items: buckets.get(label)!,
  }));
}

function statusDotClass(status: string): string {
  if (status === 'running') return 'ch-dot-blue';
  if (status === 'failed') return 'ch-dot-red';
  return 'ch-dot-green';
}

interface ConversationNodeProps {
  session: SessionSummary;
  viewMode: HarnessViewMode;
  selectedSessionId: string | null;
  searchActive: boolean;
  showProjectBadge?: boolean;
  onSelectSession: (sessionId: string) => void;
}

/**
 * 单个主对话历史窗口卡片 + 其下可折叠的同对话子代理树
 */
function ConversationNode({
  session,
  viewMode,
  selectedSessionId,
  searchActive,
  showProjectBadge = false,
  onSelectSession,
}: ConversationNodeProps) {
  const children = session.child_sessions ?? [];
  const hasSelectedChild = children.some((c) => c.session_id === selectedSessionId);
  const [manualToggle, setManualToggle] = useState<boolean | null>(null);

  // 搜索命中子代理或当前正在查看该对话下的子代理时自动展开；默认收起保持左侧极简
  const childrenExpanded =
    manualToggle !== null ? manualToggle : hasSelectedChild || searchActive;

  const isRootSelected = viewMode === 'monitor' && session.session_id === selectedSessionId;
  const childNicknames = children
    .map((c) => c.agent_nickname || (c.source_kind === 'harness' ? '续轮' : '子代理'))
    .slice(0, 3)
    .join(', ');

  return (
    <div className={`ch-conv-node ${hasSelectedChild ? 'has-active-child' : ''}`}>
      <button
        type="button"
        className={`ch-session-item ${isRootSelected ? 'is-selected' : ''}`}
        onClick={() => onSelectSession(session.session_id)}
      >
        <div className="ch-session-title">{session.title}</div>
        <div className="ch-session-meta">
          <span className={`ch-dot ${statusDotClass(session.status)}`} />
          {session.source_kind === 'harness' && <span className="ch-tag-tiny ch-tag-harness">派单</span>}
          {showProjectBadge && (
            <span className="ch-tag-tiny ch-tag-proj">{session.project_name}</span>
          )}
          {session.worktree_name && (
            <span className="ch-tag-tiny" title={`Worktree: ${session.worktree_name}`}>
              wt
            </span>
          )}
          <span className="ch-session-meta-model">{session.model || 'codex'}</span>
          <span>· {session.turn_count || 1}轮</span>
          <span>· {formatShortTimestamp(session.updated_at)}</span>
        </div>
      </button>

      {children.length > 0 && (
        <div className="ch-sub-wrap">
          <button
            type="button"
            className="ch-sub-toggle"
            onClick={() => setManualToggle(!childrenExpanded)}
            title="展开或收起同一对话窗口内派生的子代理会话"
          >
            {childrenExpanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            <GitBranch size={10} />
            <span className="ch-sub-toggle-text">
              {children.length} 个同对话子任务
              {childNicknames ? ` (${childNicknames}${children.length > 3 ? '…' : ''})` : ''}
            </span>
          </button>

          {childrenExpanded && (
            <div className="ch-sub-list">
              {children.map((child) => {
                const isChildSelected =
                  viewMode === 'monitor' && child.session_id === selectedSessionId;
                const cleanChildTitle = child.agent_nickname
                  ? child.title.replace(new RegExp(`^\\[${child.agent_nickname}\\]\\s*`), '')
                  : child.title;
                return (
                  <button
                    key={child.session_id}
                    type="button"
                    className={`ch-sub-item ${isChildSelected ? 'is-selected' : ''}`}
                    onClick={() => onSelectSession(child.session_id)}
                  >
                    <div className="ch-sub-item-head">
                      <span className={`ch-dot ${statusDotClass(child.status)}`} />
                      <span className="ch-sub-nick">
                        {child.agent_nickname || (child.source_kind === 'harness' ? '续轮派单' : '子代理')}
                      </span>
                      <span className="ch-sub-meta">
                        {child.turn_count || 1}轮 · {formatShortTimestamp(child.updated_at)}
                      </span>
                    </div>
                    <div className="ch-sub-title">{cleanChildTitle}</div>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * 左侧栏：品牌头部、三态主视图、搜索框、历史整理模式切换、以及按主对话窗口聚合的嵌套树
 */
export function SidebarTree({
  viewMode,
  onChangeView,
  searchQuery,
  onChangeSearch,
  groups,
  selectedSessionId,
  onSelectSession,
  runningCount,
}: SidebarTreeProps) {
  const [filterMode, setFilterMode] = useState<SidebarFilterMode>('project');
  const [collapsedMap, setCollapsedMap] = useState<Record<string, boolean>>({});
  const searchActive = Boolean(searchQuery.trim());

  const allRootSessions = useMemo(() => {
    const flat = groups.flatMap((g) => g.sessions);
    flat.sort(
      (a, b) =>
        (b.latest_activity_ts || b.updated_ts) - (a.latest_activity_ts || a.updated_ts),
    );
    return flat;
  }, [groups]);

  const harnessOnlySessions = useMemo(() => {
    return allRootSessions.filter(
      (s) =>
        s.source_kind === 'harness' ||
        (s.child_sessions ?? []).some((c) => c.source_kind === 'harness'),
    );
  }, [allRootSessions]);

  const timelineBuckets = useMemo(
    () => groupByTimeBuckets(allRootSessions),
    [allRootSessions],
  );

  // 默认展开包含选中项或排首位的项目文件夹
  const isGroupExpanded = useMemo(() => {
    return (group: ProjectSessionGroup, index: number): boolean => {
      if (group.project_name in collapsedMap) {
        return !collapsedMap[group.project_name];
      }
      if (searchActive) return true;
      const containsSelected = group.sessions.some(
        (s) =>
          s.session_id === selectedSessionId ||
          (s.child_sessions ?? []).some((c) => c.session_id === selectedSessionId),
      );
      if (containsSelected) return true;
      return index === 0 || group.running_count > 0;
    };
  }, [collapsedMap, searchActive, selectedSessionId]);

  const toggleGroup = (projectName: string, currentlyExpanded: boolean) => {
    setCollapsedMap((prev) => ({
      ...prev,
      [projectName]: currentlyExpanded,
    }));
  };

  return (
    <aside className="ch-rail" aria-label="Codex 会话导航">
      <div className="ch-brand">
        <BrandGlyphs />
        <div>
          <div className="ch-brand-title">Antigravity × Codex</div>
          <div className="ch-brand-sub">
            单向实时监控{runningCount > 0 ? ` · ${runningCount} 运行中` : ''}
          </div>
        </div>
      </div>

      <div className="ch-nav-tabs" role="tablist" aria-label="功能视图">
        <button
          type="button"
          role="tab"
          aria-selected={viewMode === 'monitor'}
          className={`ch-nav-tab ${viewMode === 'monitor' ? 'is-active' : ''}`}
          onClick={() => onChangeView('monitor')}
        >
          <Activity size={12} />
          <span>监控</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={viewMode === 'packets'}
          className={`ch-nav-tab ${viewMode === 'packets' ? 'is-active' : ''}`}
          onClick={() => onChangeView('packets')}
        >
          <Layers size={12} />
          <span>任务包</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={viewMode === 'dispatch'}
          className={`ch-nav-tab ${viewMode === 'dispatch' ? 'is-active' : ''}`}
          onClick={() => onChangeView('dispatch')}
        >
          <Send size={12} />
          <span>派单</span>
        </button>
      </div>

      <div className="ch-search-box">
        <Search size={13} className="ch-search-icon" />
        <input
          type="search"
          className="ch-search-input"
          placeholder="搜索对话、子代理、项目..."
          value={searchQuery}
          onChange={(e) => onChangeSearch(e.target.value)}
        />
      </div>

      {/* 历史整理切换：按项目+对话窗口归类 / 跨项目时间轴 / 仅显式派单 */}
      <div className="ch-filter-bar" role="group" aria-label="历史整理方式">
        <button
          type="button"
          className={`ch-filter-pill ${filterMode === 'project' ? 'is-active' : ''}`}
          onClick={() => setFilterMode('project')}
        >
          <Folder size={11} />
          <span>按对话树</span>
        </button>
        <button
          type="button"
          className={`ch-filter-pill ${filterMode === 'timeline' ? 'is-active' : ''}`}
          onClick={() => setFilterMode('timeline')}
        >
          <Clock size={11} />
          <span>按时间</span>
        </button>
        <button
          type="button"
          className={`ch-filter-pill ${filterMode === 'harness' ? 'is-active' : ''}`}
          onClick={() => setFilterMode('harness')}
        >
          <Send size={11} />
          <span>仅派单</span>
        </button>
      </div>

      <div className="ch-tree-scroll">
        {filterMode === 'project' &&
          (groups.length === 0 ? (
            <div className="ch-empty" style={{ minHeight: 160 }}>
              <span>暂无匹配的 Codex 会话</span>
            </div>
          ) : (
            groups.map((group, idx) => {
              const expanded = isGroupExpanded(group, idx);
              const timeBuckets = expanded ? groupByTimeBuckets(group.sessions) : [];
              return (
                <div key={group.project_name} className="ch-folder-block">
                  <button
                    type="button"
                    className="ch-folder-btn"
                    onClick={() => toggleGroup(group.project_name, expanded)}
                    title={`${group.workspace}（${group.session_count} 个主对话窗口，共 ${group.total_thread_count ?? group.session_count} 条线程）`}
                  >
                    {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                    {expanded ? <FolderOpen size={13} /> : <Folder size={13} />}
                    <span className="ch-folder-name">{group.project_name}</span>
                    {group.running_count > 0 && <span className="ch-dot ch-dot-blue" />}
                    <span className="ch-folder-count">{group.session_count}</span>
                  </button>

                  {expanded && (
                    <div className="ch-session-list">
                      {timeBuckets.map((bucket) => (
                        <div key={bucket.label} className="ch-time-group">
                          {timeBuckets.length > 1 && (
                            <div className="ch-time-divider">{bucket.label}</div>
                          )}
                          {bucket.items.map((session) => (
                            <ConversationNode
                              key={session.session_id}
                              session={session}
                              viewMode={viewMode}
                              selectedSessionId={selectedSessionId}
                              searchActive={searchActive}
                              onSelectSession={onSelectSession}
                            />
                          ))}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })
          ))}

        {filterMode === 'timeline' &&
          (timelineBuckets.length === 0 ? (
            <div className="ch-empty" style={{ minHeight: 160 }}>
              <span>暂无历史对话</span>
            </div>
          ) : (
            <div className="ch-timeline-flat">
              {timelineBuckets.map((bucket) => (
                <div key={bucket.label} className="ch-time-group">
                  <div className="ch-time-divider">{bucket.label}</div>
                  {bucket.items.map((session) => (
                    <ConversationNode
                      key={session.session_id}
                      session={session}
                      viewMode={viewMode}
                      selectedSessionId={selectedSessionId}
                      searchActive={searchActive}
                      showProjectBadge
                      onSelectSession={onSelectSession}
                    />
                  ))}
                </div>
              ))}
            </div>
          ))}

        {filterMode === 'harness' &&
          (harnessOnlySessions.length === 0 ? (
            <div className="ch-empty" style={{ minHeight: 160 }}>
              <span>暂无通过 Harness 发起的显式派单</span>
            </div>
          ) : (
            <div className="ch-timeline-flat">
              {harnessOnlySessions.map((session) => (
                <ConversationNode
                  key={session.session_id}
                  session={session}
                  viewMode={viewMode}
                  selectedSessionId={selectedSessionId}
                  searchActive={searchActive}
                  showProjectBadge
                  onSelectSession={onSelectSession}
                />
              ))}
            </div>
          ))}
      </div>
    </aside>
  );
}
