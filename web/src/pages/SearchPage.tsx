import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { useSearchDocumentsQuery, useSearchMutation } from "../features/search/queries";
import type { SearchInput, SearchResult } from "../features/search/types";
import { ResearchPageHeader } from "../components/ResearchWorkspace/ResearchPageHeader";
import { ResearchScopePanel } from "../components/ResearchWorkspace/ResearchScopePanel";
import { ResearchCommandBar } from "../components/ResearchWorkspace/ResearchCommandBar";
import { EmptyResearchState, KnowledgeContext, RetrievalProgress } from "../components/ResearchWorkspace/ResearchStates";
import { EvidenceList } from "../components/ResearchWorkspace/EvidenceList";
import { EvidenceDetail } from "../components/ResearchWorkspace/EvidenceDetail";
import { errorMessage } from "../components/ResearchWorkspace/presentation";

export function SearchPage() {
  const auth = useAuth();
  // A fresh session remounts every private draft and detail, including same-user login.
  return auth.status === "authenticated" ? <SearchWorkspace key={`${auth.username}:${auth.csrfToken}`} /> : null;
}

function SearchWorkspace() {
  const location = useLocation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const documents = useSearchDocumentsQuery();
  const [chosen, setChosen] = useState<string[]>([]);
  const linkedDocument = useRef(params.get("documents"));
  const initialPrefill =
    typeof (location.state as { prefillQuery?: unknown } | null)?.prefillQuery === "string"
      ? ((location.state as { prefillQuery: string }).prefillQuery).slice(0, 1000)
      : "";
  const [query, setQuery] = useState(initialPrefill);
  const [filter, setFilter] = useState("");
  const [limit, setLimit] = useState<SearchInput["limit"]>(10);
  const [detail, setDetail] = useState<SearchResult | null>(null);
  const [initialEditing, setInitialEditing] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const items = documents.data?.items ?? [];
  useEffect(() => {
    const incoming = (location.state as { prefillQuery?: unknown } | null)?.prefillQuery;
    if (typeof incoming !== "string") return;
    setQuery(incoming.slice(0, 1000));
    navigate(`${location.pathname}${location.search}`, { replace: true, state: null });
  }, [location.pathname, location.search, location.state, navigate]);
  useEffect(() => {
    const id = linkedDocument.current;
    if (!id || !documents.data) return;
    linkedDocument.current = null;
    if (documents.data.items.some((item) => item.document_id === id)) setChosen([id]);
  }, [documents.data]);
  const missing = chosen.some((id) => !items.some((item) => item.document_id === id));
  const scopeStamp = items.map((item) => `${item.document_id}:${item.loaded_at}`).sort().join(",");
  const fingerprint = JSON.stringify([query, chosen.slice().sort(), limit, scopeStamp, Boolean(documents.error)]);
  const search = useSearchMutation(fingerprint);
  const valid = chosen.length >= 1 && chosen.length <= 10 && query.trim().length >= 1 && query.trim().length <= 1000 && !missing && !documents.error && !documents.isPending;
  // Only a current response can open a detail or source action.
  const activeDetail = search.data?.results.find((item) => item === detail);
  const hasResults = Boolean(search.data?.results.length);
  function toggle(id: string) { setChosen((current) => current.includes(id) ? current.filter((item) => item !== id) : current.length < 10 ? [...current, id] : current); }
  function openEvidence(result: SearchResult, editing = false) {
    setInitialEditing(editing);
    setDetail(result);
  }
  function submit() {
    if (!valid || search.isPending) return;
    setDetail(null);
    setSubmitted(true);
    search.run({ query, document_ids: chosen, limit });
  }
  return <article className="search-page">
    <ResearchPageHeader />
    <div className="search-layout">
      <ResearchScopePanel items={items} chosen={chosen} filter={filter} onFilter={setFilter}
        onToggle={toggle} onClear={() => setChosen([])} loading={documents.isPending}
        error={Boolean(documents.error)} missing={missing} onRetry={() => void documents.refetch()} />
      <section className="search-content" aria-label="文档检索">
        <ResearchCommandBar query={query} onQuery={setQuery} limit={limit} onLimit={setLimit}
          valid={valid} loading={search.isPending} onSubmit={submit} />
        <RetrievalProgress loading={search.isPending} error={Boolean(search.error)} data={search.data}
          submitted={submitted} selectedCount={chosen.length} />
        {search.error ? <div className="search-error" role="alert"><p>{errorMessage(search.error, "检索暂不可用，请稍后重试。")}</p>{search.error instanceof ApiError && search.error.retryable ? <p>可稍后点击“检索证据”重试，不会自动重复请求。</p> : <p>请检查检索范围后重新提交。</p>}</div> : null}
        {!search.isPending && !search.error && !search.data ? <EmptyResearchState onSuggestion={setQuery} /> : null}
        {search.data && !hasResults ? <div className="search-empty"><h2>未找到相关片段</h2><p>试试更具体的关键词，或调整所选文档范围。</p></div> : null}
        {hasResults ? <><EvidenceList results={search.data!.results} query={query} onOpen={openEvidence} /><KnowledgeContext /></> : null}
      </section>
    </div>
    {activeDetail ? <EvidenceDetail key={`${search.data!.request_id}:${activeDetail.rank}:${initialEditing}`} result={activeDetail} initialEditing={initialEditing} onClose={() => setDetail(null)} /> : null}
  </article>;
}
