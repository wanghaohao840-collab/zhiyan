import { Plus } from "lucide-react";
import type { ChangeEvent } from "react";

import { Button } from "../Button/Button";
import { TextField } from "../TextField/TextField";

const DOCUMENT_TYPE_TABS = [
  { id: "all", label: "全部" },
  { id: "pdf", label: "PDF" },
  { id: "md", label: "Markdown" },
  { id: "docx", label: "DOCX" },
  { id: "txt", label: "TXT" },
] as const;

export type DocumentTypeFilter = (typeof DOCUMENT_TYPE_TABS)[number]["id"];

type DocumentToolbarProps = {
  filter: string;
  onFilterChange: (value: string) => void;
  onOpenImport: (trigger: HTMLButtonElement) => void;
  showFilter: boolean;
  typeFilter?: DocumentTypeFilter;
  onTypeFilterChange?: (value: DocumentTypeFilter) => void;
};

export function DocumentToolbar({
  filter,
  onFilterChange,
  onOpenImport,
  showFilter,
  typeFilter = "all",
  onTypeFilterChange,
}: DocumentToolbarProps) {
  function handleFilterChange(event: ChangeEvent<HTMLInputElement>) {
    onFilterChange(event.currentTarget.value);
  }

  return (
    <header className="document-toolbar">
      <h1>文档库</h1>
      <p>每一次阅读，都有迹可循</p>
      <Button
        className="document-toolbar__import"
        onClick={(event) => onOpenImport(event.currentTarget)}
      >
        <Plus size={16} strokeWidth={1.6} aria-hidden="true" />
        导入文档
      </Button>
      {showFilter ? (
        <div className="document-toolbar__controls">
          {onTypeFilterChange ? (
            <div className="document-type-tabs" role="group" aria-label="按文档类型筛选">
              {DOCUMENT_TYPE_TABS.map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  data-text={tab.label}
                  className={`document-type-tab${typeFilter === tab.id ? " is-active" : ""}`}
                  aria-pressed={typeFilter === tab.id}
                  onClick={() => onTypeFilterChange(tab.id)}
                >
                  {tab.label}
                </button>
              ))}
            </div>
          ) : null}
          <TextField
            className="document-toolbar__filter-input"
            label="按名称筛选"
            placeholder="输入文件名"
            type="search"
            value={filter}
            onChange={handleFilterChange}
          />
        </div>
      ) : null}
    </header>
  );
}
