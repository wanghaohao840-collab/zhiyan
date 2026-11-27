import { useMemo, useState } from 'react';
import {
  Activity,
  ChevronDown,
  ChevronRight,
  Folder,
  FolderOpen,
  Layers,
  Search,
  Send,
} from 'lucide-react';
import type { HarnessViewMode, ProjectSessionGroup } from '../types/harness';
import { formatShortTimestamp } from '../utils/format';

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
 * 左侧栏：品牌头部、三态视图切换、会话搜索框以及按项目折叠的 Codex 会话树
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
  const [collapsedMap, setCollapsedMap] = useState<Record<string, boolean>>({});

  // 默认展开第一个项目组以及包含当前选中会话的项目组
  const isGroupExpanded = useMemo(() => {
    return (group: ProjectSessionGroup, index: number): boolean => {
      if (group.project_name in collapsedMap) {
        return !collapsedMap[group.project_name];
      }
      if (searchQuery.trim()) return true;
      if (group.sessions.some((s) => s.session_id === selectedSessionId)) return true;
      return index === 0 || group.running_count > 0;
    };
  }, [collapsedMap, searchQuery, selectedSessionId]);

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
          placeholder="搜索对话、项目、模型..."
          value={searchQuery}
          onChange={(e) => onChangeSearch(e.target.value)}
        />
      </div>

      <div className="ch-tree-scroll">
        {groups.length === 0 ? (
          <div className="ch-empty" style={{ minHeight: 160 }}>
            <span>暂无匹配的 Codex 会话</span>
          </div>
        ) : (
          groups.map((group, idx) => {
            const expanded = isGroupExpanded(group, idx);
            return (
              <div key={group.project_name} className="ch-folder-block">
                <button
                  type="button"
                  className="ch-folder-btn"
                  onClick={() => toggleGroup(group.project_name, expanded)}
                  title={group.workspace}
                >
                  {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                  {expanded ? <FolderOpen size={13} /> : <Folder size={13} />}
                  <span className="ch-folder-name">{group.project_name}</span>
                  {group.running_count > 0 && <span className="ch-dot ch-dot-blue" />}
                  <span className="ch-folder-count">{group.session_count}</span>
                </button>

                {expanded && (
                  <div className="ch-session-list">
                    {group.sessions.map((session) => {
                      const selected =
                        viewMode === 'monitor' && session.session_id === selectedSessionId;
                      const dotClass =
                        session.status === 'running'
                          ? 'ch-dot-blue'
                          : session.status === 'failed'
                            ? 'ch-dot-red'
                            : 'ch-dot-green';
                      return (
                        <button
                          key={session.session_id}
                          type="button"
                          className={`ch-session-item ${selected ? 'is-selected' : ''}`}
                          onClick={() => onSelectSession(session.session_id)}
                        >
                          <div className="ch-session-title">{session.title}</div>
                          <div className="ch-session-meta">
                            <span className={`ch-dot ${dotClass}`} />
                            <span>{session.source_kind === 'harness' ? '→' : '·'}</span>
                            <span className="ch-session-meta-model">
                              {session.model || 'codex'}
                            </span>
                            <span>· {session.turn_count || 1}轮</span>
                            <span>· {formatShortTimestamp(session.updated_at)}</span>
                          </div>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}
