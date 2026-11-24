import { ArrowUpRight } from "lucide-react";

export function MigrationEmptyState() {
  return (
    <section className="migration-state" aria-label="能力迁移中">
      <span className="migration-state__icon" aria-hidden="true">
        <ArrowUpRight size={20} strokeWidth={1.6} aria-hidden="true" />
      </span>
      <h2>能力迁移中</h2>
      <p>该能力正在迁移到新版界面，可暂时前往旧版使用。</p>
      <a className="migration-state__action" href="/legacy/" aria-label="前往旧版">
        前往旧版 · /legacy/
      </a>
    </section>
  );
}
