import { FileText } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import type { Document } from "../../features/documents/types";
import { Button } from "../Button/Button";

function DocumentScopeItem({ item, selected, disabled, onToggle }: {
  item: Document; selected: boolean; disabled: boolean; onToggle: () => void;
}) {
  const format = item.file_suffix?.replace(/^\./, "").toUpperCase() || "文档";
  return <label className={`search-document${selected ? " is-selected" : ""}`}>
    <input type="checkbox" aria-label={item.name} checked={selected} disabled={disabled} onChange={onToggle} />
    <FileText size={18} strokeWidth={1.6} aria-hidden="true" className="scope-file-icon" />
    <span className="scope-file-text"><strong title={item.name}>{item.name}</strong><small>{format}{item.size_bytes != null ? ` · ${Math.max(1, Math.round(item.size_bytes / 1024))} KB` : ""}</small></span>
    <span className="scope-check" aria-hidden="true">{selected ? "✓" : ""}</span>
  </label>;
}

export function ResearchScopePanel({ items, chosen, filter, onFilter, onToggle, onClear, loading, error, missing, onRetry }: {
  items: Document[]; chosen: string[]; filter: string; onFilter: (value: string) => void;
  onToggle: (id: string) => void; onClear: () => void; loading: boolean; error: boolean; missing: boolean; onRetry: () => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const filtered = items.filter((item) => item.name.toLocaleLowerCase().includes(filter.trim().toLocaleLowerCase()));
  return <aside className="search-scope" aria-labelledby="search-scope-heading">
    <div className="scope-heading"><h2 id="search-scope-heading">研究范围 <span>{chosen.length} / 10</span></h2>
      <button className="scope-collapse" aria-expanded={expanded} aria-controls="research-scope-body" onClick={() => setExpanded(!expanded)}>{expanded ? "收起" : "展开"}</button></div>
    <div id="research-scope-body" className={expanded ? "scope-body" : "scope-body is-collapsed"}>
      <p className="scope-description">仅在选中的资料中检索<br /><span>不会访问互联网</span></p>
      <label className="sr-only" htmlFor="search-document-filter">筛选文档</label>
      <input id="search-document-filter" value={filter} onChange={(event) => onFilter(event.target.value)} placeholder="搜索资料..." />
      {loading ? <p role="status">正在加载文档…</p> : error ? <div role="alert"><p>文档库暂不可用，无法确认检索范围。</p><Button hierarchy="secondary" onClick={onRetry}>重新加载文档</Button></div> : !items.length ? <div className="scope-empty"><p>还没有可检索的文档</p><Link to="/documents">到文档库导入资料 →</Link></div> : <fieldset><legend className="sr-only">选择检索文档</legend>
        <div className="search-document-list">{filtered.map((item) => <DocumentScopeItem key={item.document_id} item={item} selected={chosen.includes(item.document_id)} disabled={!chosen.includes(item.document_id) && chosen.length >= 10} onToggle={() => onToggle(item.document_id)} />)}</div>
        {!filtered.length ? <p>没有匹配的资料，试试其他名称。</p> : null}
      </fieldset>}
      <footer className="scope-footer"><span>已选择 {chosen.length} 份资料</span>{chosen.length ? <Button hierarchy="ghost" onClick={onClear}>清除选择</Button> : null}</footer>
      {missing ? <p role="alert">所选文档已不在文档库中，请重新选择。</p> : null}
    </div>
  </aside>;
}
