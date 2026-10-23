"""Immutable completed-only first-user initialization ledger."""

from alembic import op

from app.isolated_resource_settings import LEDGER_CONSTRAINTS, TRUSTED_INIT_GUARD_BODY


revision = "20261022_17"
down_revision = "20261007_16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""create table trusted_user_initializations (
        operation_id uuid not null,
        format_version integer not null,
        user_id text not null,
        username_key text not null,
        resource_descriptor jsonb not null,
        resource_descriptor_sha256 text not null,
        history_initial_sha256 text not null,
        memory_initial_sha256 text not null,
        rag_identity jsonb not null,
        episode_identity jsonb not null,
        rag_kind text not null,
        episode_kind text not null,
        rag_namespace text not null,
        episode_namespace text not null,
        rag_index_key text not null,
        episode_index_key text not null,
        rag_initial_generation_id uuid not null,
        episode_initial_generation_id uuid not null,
        rag_head_revision bigint not null,
        episode_head_revision bigint not null,
        rag_index_revision bigint not null,
        episode_index_revision bigint not null,
        history_snapshot_version bigint not null,
        memory_snapshot_version bigint not null,
        empty_manifest_sha256 text not null,
        history_document_evidence_sha256 text not null,
        completed_at timestamptz not null default clock_timestamp(),
        constraint trusted_user_initializations_pkey primary key (operation_id)
    )""")
    for name, definition in LEDGER_CONSTRAINTS.items():
        if name == "trusted_user_initializations_pkey":
            continue
        op.execute(f"alter table trusted_user_initializations add constraint {name} {definition}")
    op.execute("create function trusted_init_insert_guard_17() returns trigger "
               "language plpgsql as $trusted$" + TRUSTED_INIT_GUARD_BODY + "$trusted$")
    op.execute("""create trigger trusted_init_guard_17
        before insert or update or delete on trusted_user_initializations
        for each row execute function trusted_init_insert_guard_17()""")


def downgrade() -> None:
    if op.get_bind().exec_driver_sql(
            "select exists(select 1 from trusted_user_initializations)").scalar():
        raise RuntimeError("Trusted initialization history cannot be discarded")
    op.execute("drop trigger trusted_init_guard_17 on trusted_user_initializations")
    op.execute("drop function trusted_init_insert_guard_17()")
    op.execute("drop table trusted_user_initializations")
