export function NoteEditorTabs({ mode, onChange }: { mode: "edit" | "preview"; onChange: (mode: "edit" | "preview") => void }) {
  return (
    <div className="notes-editor__tabs" role="tablist" aria-label="笔记内容模式">
      <button
        type="button"
        role="tab"
        data-text="编辑"
        aria-selected={mode === "edit"}
        onClick={() => onChange("edit")}
      >
        编辑
      </button>
      <button
        type="button"
        role="tab"
        data-text="预览"
        aria-selected={mode === "preview"}
        onClick={() => onChange("preview")}
      >
        预览
      </button>
    </div>
  );
}
