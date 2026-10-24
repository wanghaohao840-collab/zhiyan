"""PostgreSQL authority for whole-corpus immutable vector generations.

No vector I/O occurs here. This is a foundation: readers and writers must use a
generation-aware storage adapter before the pointer can fence external bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from uuid import UUID, uuid4

from app.history import EMPTY_HISTORY
from app.postgres_coordination import PostgresUserMutationCoordinator, UserMutationLease
from app.postgres_import_leases import ImportAttempt, PostgresImportLeaseRepository
from hello_agents.memory.rag.index_identity import IndexIdentity

_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_EMPTY_DIGEST = hashlib.sha256(b'[]').hexdigest()


class VectorAuthorityError(RuntimeError):
    """The requested generation transition or scope is not authorized."""


def validate_import_pair_for_recovery_in_transaction(cursor, intent: dict) -> tuple:
    """Lock and validate both candidates; this helper has no write authority."""
    attempt = intent['attempt']
    locked = []
    for kind in ('rag', 'episode'):
        scope = intent['scopes'][kind]
        generation_id = UUID(scope['candidate_id'])
        reservation = cursor.execute('''select * from generation_reservations
            where generation_id=%s for update''', (generation_id,)).fetchone()
        if (reservation is None or
            tuple(reservation[name] for name in (
                'user_id','task_id','task_lease_version','vector_kind',
                'namespace','index_key','base_revision','owner',
                'user_lease_token','user_lease_version','state')) !=
            (attempt['user_id'],attempt['task_id'],attempt['task_lease_version'],
             kind,scope['namespace'],scope['index_key'],scope['head']['revision'],
             attempt['worker_id'],UUID(attempt['user_lease_token']),
             attempt['user_lease_version'],'reserved') or
            reservation['revoked_at'] is not None):
            raise VectorAuthorityError('Exact generation reservation differs')
        generation = cursor.execute('''select * from vector_generations
            where generation_id=%s for update''', (generation_id,)).fetchone()
        if generation is not None:
            if (tuple(generation[name] for name in (
                    'tenant_id','vector_kind','namespace','index_key',
                    'base_revision','index_revision','owner',
                    'user_lease_token','user_lease_version','task_id',
                    'task_lease_token','task_lease_version')) !=
                (attempt['user_id'],kind,scope['namespace'],scope['index_key'],
                 scope['head']['revision'],scope['index_revision'],
                 attempt['worker_id'],UUID(attempt['user_lease_token']),
                 attempt['user_lease_version'],attempt['task_id'],
                 UUID(attempt['task_lease_token']),attempt['task_lease_version'])
                or generation['state'] not in ('staging','sealed')
                or generation['publication_revision'] is not None
                or generation['publication_snapshot_version'] is not None
                or generation['published_at'] is not None):
                raise VectorAuthorityError('Exact generation ownership or state differs')
        locked.append((generation_id,generation is not None))
    return tuple(locked)


@dataclass(frozen=True)
class VectorScope:
    tenant_id: str
    vector_kind: str
    namespace: str
    identity: IndexIdentity

    def __post_init__(self):
        if (not isinstance(self.tenant_id, str) or not self.tenant_id.strip()
                or self.vector_kind not in ('rag', 'episode')
                or not isinstance(self.namespace, str) or not self.namespace.strip()
                or not isinstance(self.identity, IndexIdentity)
                or self.identity.backend != 'qdrant'):
            raise ValueError('Invalid vector scope or index identity')

    @property
    def index_key(self):
        return hashlib.sha256(_canonical(self.identity.to_dict()).encode()).hexdigest()

    @property
    def key(self):
        return (self.tenant_id, self.vector_kind, self.namespace, self.index_key)


@dataclass(frozen=True)
class VectorHead:
    state: str  # missing, empty, or published
    revision: int | None
    generation_id: UUID | None
    index_revision: int | None
    snapshot_version: int | None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _check_revision(value):
    if value is not None and (type(value) is not int or value < 1):
        raise ValueError('Expected head revision must be positive or None')


class PostgresVectorGenerationAuthority:
    def __init__(self, database):
        self.database = database
        self.coordinator = PostgresUserMutationCoordinator(database)
        self.imports = PostgresImportLeaseRepository(database)

    def _owner(self, cursor, authority, scope):
        self.coordinator._isolation(cursor)
        if isinstance(authority, ImportAttempt):
            self.imports._live(cursor, authority)
            lease = authority.user_lease
            task = (authority.task.task_id, authority.lease_token, authority.lease_version)
        elif isinstance(authority, UserMutationLease):
            self.coordinator.require_live_in_transaction(cursor, authority)
            lease = authority
            task = (None, None, None)
        else:
            raise VectorAuthorityError('A live user lease or import attempt is required')
        if lease.user_id != scope.tenant_id:
            raise VectorAuthorityError('Wrong tenant')
        return lease, task

    @staticmethod
    def _index(cursor, scope):
        row = cursor.execute('''select identity,index_revision from vector_indexes
            where tenant_id=%s and vector_kind=%s and namespace=%s and index_key=%s''', scope.key).fetchone()
        if row is None or row['identity'] != scope.identity.to_dict():
            raise VectorAuthorityError('Missing or incompatible index identity')
        return row['index_revision']

    @staticmethod
    def _head_row(cursor, scope, *, lock=False):
        return cursor.execute('''select * from vector_heads where tenant_id=%s
            and vector_kind=%s and namespace=%s and index_key=%s'''
            + (' for update' if lock else ''), scope.key).fetchone()

    def read_head(self, scope):
        """Resolve a pinned pointer. Missing authority never means an empty corpus."""
        with self.database.transaction() as cursor:
            self.coordinator._isolation(cursor)
            # One READ COMMITTED statement pins index, head and manifest to one
            # database snapshot even when a concurrent publisher retires a head.
            row = cursor.execute('''select i.identity,i.index_revision,
                h.revision,h.generation_id,h.last_generation_id,
                h.index_revision as head_index_revision,h.snapshot_version,
                g.state as generation_state,g.expected_count
                from vector_indexes i
                left join vector_heads h on h.tenant_id=i.tenant_id
                    and h.vector_kind=i.vector_kind and h.namespace=i.namespace
                    and h.index_key=i.index_key
                left join vector_generations g on g.generation_id=h.last_generation_id
                    and g.tenant_id=i.tenant_id and g.vector_kind=i.vector_kind
                    and g.namespace=i.namespace and g.index_key=i.index_key
                where i.tenant_id=%s and i.vector_kind=%s
                    and i.namespace=%s and i.index_key=%s''', scope.key).fetchone()
            if row is None:
                return VectorHead('missing', None, None, None, None)
            if row['identity'] != scope.identity.to_dict():
                raise VectorAuthorityError('Incompatible index identity')
            if row['revision'] is None:
                return VectorHead('missing', None, None, row['index_revision'], None)
            if row['head_index_revision'] != row['index_revision']:
                raise VectorAuthorityError('Head index revision differs')
            generation = row['generation_id']
            if (row['generation_state'] != 'published'
                    or (generation is None) != (row['expected_count'] == 0)
                    or (generation is not None and generation != row['last_generation_id'])):
                raise VectorAuthorityError('Published generation is invalid')
            return VectorHead('empty' if generation is None else 'published', row['revision'],
                              generation, row['head_index_revision'], row['snapshot_version'])

    def require_candidate(self, live, scope, generation_id, *, operation):
        from app.import_memory_publication import _require_live_issuer
        from app.import_publication_evidence import PostgresImportPublicationEvidenceRepository
        _, issue = _require_live_issuer(live)
        selected = issue.rag_service if scope.vector_kind == 'rag' else issue.episode_service
        if self is not selected.authority:
            raise VectorAuthorityError('Candidate database differs')
        evidence_repository = PostgresImportPublicationEvidenceRepository(self.database)
        with self.database.transaction() as cursor:
            cursor.execute('begin')
            evidence = evidence_repository._read_exact_in_cursor(cursor, issue.key)
            if evidence is None:
                raise VectorAuthorityError('Candidate evidence is missing')
            evidence_repository.require_candidate_in_transaction(cursor, live,
                scope.vector_kind, scope, generation_id, operation=operation,
                expected_phase=evidence.phase, expected_version=evidence.phase_version)

    def stage(self, scope, authority, *, expected_revision, generation_id=None,
              live=None):
        """Allocate a never-reused candidate, bound to the current complete head."""
        _check_revision(expected_revision)
        generation_id = uuid4() if generation_id is None else generation_id
        if not isinstance(generation_id, UUID):
            raise ValueError('generation_id must be UUID')
        with self.database.transaction() as cursor:
            cursor.execute('begin')
            return self._stage_in_transaction(cursor, scope, authority,
                expected_revision=expected_revision, generation_id=generation_id, live=live)

    def _stage_in_transaction(self, cursor, scope, authority, *, expected_revision,
                              generation_id: UUID, live=None) -> UUID:
        lease, task = self._owner(cursor, authority, scope)
        if live is None:
            from app.import_publication_evidence import require_no_gate_in_transaction
            require_no_gate_in_transaction(cursor, scope.tenant_id)
            if cursor.execute('''select 1 from generation_reservations
                where generation_id=%s''',(generation_id,)).fetchone() is not None:
                raise VectorAuthorityError('Candidate UUID is permanently reserved')
        else:
            from app.import_memory_publication import _require_live_issuer
            from app.import_publication_evidence import PostgresImportPublicationEvidenceRepository
            evidence_repository = PostgresImportPublicationEvidenceRepository(self.database)
            evidence = evidence_repository._read_exact_in_cursor(cursor,
                _require_live_issuer(live)[1].key)
            evidence_repository.require_candidate_in_transaction(cursor, live,
                scope.vector_kind, scope, generation_id, operation='stage_before',
                expected_phase=evidence.phase, expected_version=evidence.phase_version)
        index = cursor.execute('''select identity,index_revision from vector_indexes
            where tenant_id=%s and vector_kind=%s and namespace=%s
            and index_key=%s''', scope.key).fetchone()
        if index is None:
            cursor.execute('''insert into vector_indexes
                (tenant_id,vector_kind,namespace,index_key,identity)
                values (%s,%s,%s,%s,%s::jsonb)''',
                (*scope.key,_canonical(scope.identity.to_dict())))
        index_revision = self._index(cursor, scope)
        head = self._head_row(cursor, scope, lock=True)
        if (None if head is None else head['revision']) != expected_revision:
            raise VectorAuthorityError('Head revision changed')
        cursor.execute('''insert into vector_generations
            (generation_id,tenant_id,vector_kind,namespace,index_key,base_revision,
             index_revision,owner,user_lease_token,user_lease_version,
             task_id,task_lease_token,task_lease_version,state)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'staging')''',
            (generation_id, *scope.key, expected_revision, index_revision,
             lease.owner, lease.lease_token, lease.lease_version, *task))
        self._owner(cursor, authority, scope)
        if live is not None:
            evidence_repository.require_candidate_in_transaction(cursor, live,
                scope.vector_kind, scope, generation_id, operation='stage_after',
                expected_phase=evidence.phase, expected_version=evidence.phase_version)
        return generation_id

    def _candidate(self, cursor, scope, authority, generation_id, *, states):
        lease, task = self._owner(cursor, authority, scope)
        row = cursor.execute('''select * from vector_generations where generation_id=%s
            and tenant_id=%s and vector_kind=%s and namespace=%s and index_key=%s
            for update''', (generation_id, *scope.key)).fetchone()
        if (row is None or row['state'] not in states or row['owner'] != lease.owner
                or row['user_lease_token'] != lease.lease_token
                or row['user_lease_version'] != lease.lease_version
                or (row['task_id'], row['task_lease_token'], row['task_lease_version']) != task):
            raise VectorAuthorityError('Candidate ownership or state changed')
        self._owner(cursor, authority, scope)  # fresh clock after candidate lock
        return row

    def seal(self, scope, authority, generation_id, *, expected_count, content_digest,
             live=None):
        if (type(expected_count) is not int or expected_count < 0
                or not isinstance(content_digest, str) or not _DIGEST.fullmatch(content_digest)):
            raise ValueError('Verified count and SHA-256 digest are required')
        with self.database.transaction() as cursor:
            cursor.execute('begin')
            self._seal_in_transaction(cursor, scope, authority, generation_id,
                expected_count=expected_count, content_digest=content_digest, live=live)

    def _seal_in_transaction(self, cursor, scope, authority, generation_id: UUID, *,
                             expected_count: int, content_digest: str, live=None) -> None:
        self._candidate(cursor, scope, authority, generation_id, states=('staging',))
        if live is None:
            from app.import_publication_evidence import require_no_gate_in_transaction
            require_no_gate_in_transaction(cursor, scope.tenant_id)
        else:
            from app.import_memory_publication import _require_live_issuer
            from app.import_publication_evidence import PostgresImportPublicationEvidenceRepository
            repository = PostgresImportPublicationEvidenceRepository(self.database)
            evidence = repository._read_exact_in_cursor(cursor,
                _require_live_issuer(live)[1].key)
            repository.require_candidate_in_transaction(cursor, live,
                scope.vector_kind, scope, generation_id, operation='seal',
                expected_phase=evidence.phase, expected_version=evidence.phase_version)
        cursor.execute('''update vector_generations set state='sealed', expected_count=%s,
            content_digest=%s,sealed_at=clock_timestamp() where generation_id=%s''',
            (expected_count, content_digest, generation_id))
        self._owner(cursor, authority, scope)

    def _publish_initial_empty_pair_in_transaction(
            self, cursor, lease: UserMutationLease, rag_scope: VectorScope,
            episode_scope: VectorScope) -> tuple[UUID, UUID]:
        """Publish both empty baselines on the caller's uncommitted cursor."""
        if (not isinstance(lease, UserMutationLease)
                or not isinstance(rag_scope, VectorScope)
                or not isinstance(episode_scope, VectorScope)
                or rag_scope.tenant_id != lease.user_id
                or episode_scope.tenant_id != lease.user_id
                or rag_scope.vector_kind != 'rag'
                or rag_scope.namespace != 'pdf_' + lease.user_id
                or episode_scope.vector_kind != 'episode'
                or episode_scope.namespace != 'episodes'
                or rag_scope.identity.base_collection == episode_scope.identity.base_collection
                or rag_scope.identity.physical_collection == episode_scope.identity.physical_collection):
            raise VectorAuthorityError('Initial vector scopes or lease differ')

        # The lease check locks the user before any vector authority rows.
        self._owner(cursor, lease, rag_scope)
        from app.import_publication_evidence import require_no_gate_in_transaction
        require_no_gate_in_transaction(cursor, lease.user_id)
        snapshots = cursor.execute('''select kind,version,payload from user_snapshots
            where user_id=%s and kind in ('history','memory')''',
            (lease.user_id,)).fetchall()
        by_kind = {row['kind']: row for row in snapshots}
        if (len(snapshots) != 2 or set(by_kind) != {'history', 'memory'}
                or by_kind['history']['version'] != 1
                or by_kind['history']['payload'] != EMPTY_HISTORY
                or by_kind['memory']['version'] != 1
                or by_kind['memory']['payload'] != {
                    'user_id': lease.user_id, 'memories': []}):
            raise VectorAuthorityError('Initial snapshots must be exact empty v1 values')
        existing_index = cursor.execute('''select 1 from vector_indexes
            where tenant_id=%s limit 1''', (lease.user_id,)).fetchone()
        existing_head = cursor.execute('''select 1 from vector_heads
            where tenant_id=%s limit 1''', (lease.user_id,)).fetchone()
        if existing_index is not None or existing_head is not None:
            raise VectorAuthorityError('Vector authority already exists for user')

        rag_id = uuid4()
        episode_id = uuid4()
        while episode_id == rag_id:
            episode_id = uuid4()
        for scope, generation_id in ((rag_scope, rag_id), (episode_scope, episode_id)):
            self._stage_in_transaction(cursor, scope, lease,
                expected_revision=None, generation_id=generation_id)
        for scope, generation_id in ((rag_scope, rag_id), (episode_scope, episode_id)):
            self._seal_in_transaction(cursor, scope, lease, generation_id,
                expected_count=0, content_digest=_EMPTY_DIGEST)
        for scope, generation_id in ((rag_scope, rag_id), (episode_scope, episode_id)):
            self._publish(cursor, scope, lease, generation_id,
                expected_revision=None, expected_index_revision=1,
                snapshot_version=1, expected_count=0, content_digest=_EMPTY_DIGEST)
        self._owner(cursor, lease, rag_scope)
        require_no_gate_in_transaction(cursor, lease.user_id)
        return rag_id, episode_id

    def abandon(self, scope, authority, generation_id):
        """Permanently revoke a candidate after failed or ambiguous external I/O."""
        with self.database.transaction() as cursor:
            cursor.execute('begin')
            self._owner(cursor, authority, scope)
            from app.import_publication_evidence import require_no_gate_in_transaction
            require_no_gate_in_transaction(cursor, scope.tenant_id)
            self._candidate(cursor, scope, authority, generation_id, states=('staging', 'sealed'))
            cursor.execute("""update vector_generations set state='abandoned',
                abandoned_at=clock_timestamp() where generation_id=%s""", (generation_id,))
            self._owner(cursor, authority, scope)

    def _publish(self, cursor, scope, authority, generation_id, *, expected_revision,
                 expected_index_revision, snapshot_version, expected_count=None,
                 content_digest=None, admission=None):
        _check_revision(expected_revision)
        if type(expected_index_revision) is not int or expected_index_revision < 1:
            raise ValueError('Expected index revision is required')
        if snapshot_version is not None and (type(snapshot_version) is not int or snapshot_version < 0):
            raise ValueError('Invalid snapshot version')
        self._owner(cursor, authority, scope)
        if admission is None:
            from app.import_publication_evidence import require_no_gate_in_transaction
            require_no_gate_in_transaction(cursor,scope.tenant_id)
        else:
            from app.import_memory_publication import _require_terminal_admission
            _require_terminal_admission(admission,cursor,'vector_publish',self,
                scope,authority,generation_id,expected_revision,
                expected_index_revision,snapshot_version,expected_count,content_digest)
        index_revision = self._index(cursor, scope)
        if index_revision != expected_index_revision:
            raise VectorAuthorityError('Index revision changed')
        head = self._head_row(cursor, scope, lock=True)
        if (None if head is None else head['revision']) != expected_revision:
            raise VectorAuthorityError('Head revision changed')
        candidate = self._candidate(cursor, scope, authority, generation_id, states=('sealed',))
        if admission is None:
            from app.import_publication_evidence import require_no_gate_in_transaction
            require_no_gate_in_transaction(cursor,scope.tenant_id)
        else:
            from app.import_memory_publication import _require_terminal_admission
            _require_terminal_admission(admission,cursor,'vector_publish',self,
                scope,authority,generation_id,expected_revision,
                expected_index_revision,snapshot_version,expected_count,content_digest)
        if (candidate['base_revision'] != expected_revision
                or candidate['index_revision'] != expected_index_revision
                or (expected_count is not None and candidate['expected_count'] != expected_count)
                or (content_digest is not None and candidate['content_digest'] != content_digest)):
            raise VectorAuthorityError('Candidate base/index revision changed')
        # The zero-point corpus has an explicit head row but no visible generation.
        new_generation = None if candidate['expected_count'] == 0 else generation_id
        revision = 1 if expected_revision is None else expected_revision + 1
        if head is None:
            cursor.execute('''insert into vector_heads
                (tenant_id,vector_kind,namespace,index_key,revision,generation_id,
                 last_generation_id,index_revision,snapshot_version)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (*scope.key, revision, new_generation, generation_id, index_revision, snapshot_version))
        else:
            cursor.execute('''update vector_heads set revision=%s,generation_id=%s,
                last_generation_id=%s,index_revision=%s,snapshot_version=%s,
                updated_at=clock_timestamp()
                where tenant_id=%s and vector_kind=%s and namespace=%s and index_key=%s''',
                (revision, new_generation, generation_id, index_revision, snapshot_version, *scope.key))
            cursor.execute("update vector_generations set state='retired' where generation_id=%s and state='published'",
                           (head['last_generation_id'],))
        cursor.execute("""update vector_generations set state='published',
            publication_revision=%s,publication_snapshot_version=%s,
            published_at=clock_timestamp() where generation_id=%s""",
            (revision, snapshot_version, generation_id))
        self._owner(cursor, authority, scope)
        return revision

    def publish_user(self, scope, lease, generation_id, *, expected_revision,
                     expected_index_revision=1, snapshot_version=None, domain_publish=None):
        """Publish a synchronous mutation under the user lease in one transaction."""
        if not isinstance(lease, UserMutationLease):
            raise VectorAuthorityError('Direct publication requires a user lease')
        with self.coordinator.publication(lease) as cursor:
            revision = self._publish(cursor, scope, lease, generation_id,
                expected_revision=expected_revision, expected_index_revision=expected_index_revision,
                snapshot_version=snapshot_version)
            if domain_publish is not None:
                domain_publish(cursor)
            self._owner(cursor, lease, scope)
            return revision

    def complete_import(self, scope, attempt, generation_id, *, expected_revision,
                        expected_index_revision=1, snapshot_version=None, domain_publish=None):
        """Publish only through import completion's caller-owned PG transaction."""
        if not isinstance(attempt, ImportAttempt):
            raise VectorAuthorityError('Import publication requires an attempt')
        result = []
        def publish(cursor):
            result.append(self._publish(cursor, scope, attempt, generation_id,
                expected_revision=expected_revision, expected_index_revision=expected_index_revision,
                snapshot_version=snapshot_version))
            if domain_publish is not None:
                domain_publish(cursor)
        task = self.imports.complete(attempt, publish)
        return task, result[0]

    def reconcile(self, scope, generation_id):
        """Read durable outcome after an ambiguous PG commit response; never replay."""
        if not isinstance(generation_id, UUID):
            raise ValueError('generation_id must be UUID')
        with self.database.transaction() as cursor:
            row = cursor.execute('''select g.state,g.expected_count,g.content_digest,
                h.last_generation_id,h.revision as head_revision
                from vector_generations g
                left join vector_heads h on h.tenant_id=g.tenant_id
                    and h.vector_kind=g.vector_kind and h.namespace=g.namespace
                    and h.index_key=g.index_key
                where g.generation_id=%s and g.tenant_id=%s and g.vector_kind=%s
                    and g.namespace=%s and g.index_key=%s''',
                (generation_id, *scope.key)).fetchone()
            if row is None:
                return None
            return {'state': row['state'], 'expected_count': row['expected_count'],
                    'content_digest': row['content_digest'],
                    'is_head': row['last_generation_id'] == generation_id,
                    'head_revision': row['head_revision']}

    def publication_receipt(self, scope, generation_id):
        """One PG snapshot of immutable publication and exact import completion."""
        if not isinstance(generation_id, UUID):
            raise ValueError('generation_id must be UUID')
        with self.database.transaction() as cursor:
            return self._receipts_in_cursor(cursor, ((scope, generation_id),))[0]

    @staticmethod
    def _receipts_in_cursor(cursor, requests):
        """Pure SELECT; callers may supply a read-only repeatable-read cursor.

        A pair is projected by one statement and therefore one MVCC snapshot.
        The caller checks every field; absent rows remain absent.
        """
        if len(requests) not in (1, 2) or any(
                not isinstance(scope, VectorScope) or not isinstance(generation_id, UUID)
                for scope, generation_id in requests):
            raise ValueError('One or two exact scope/generation selectors are required')
        selectors = ' or '.join(
            '(g.generation_id=%s and g.tenant_id=%s and g.vector_kind=%s '
            'and g.namespace=%s and g.index_key=%s)' for _ in requests)
        parameters = tuple(value for scope, generation_id in requests
                           for value in (generation_id, *scope.key))
        rows = cursor.execute(f'''select g.generation_id,g.tenant_id,g.vector_kind,
                g.namespace,g.index_key,i.identity,g.sealed_at,
                g.state,g.base_revision,g.index_revision,
                g.publication_revision,g.publication_snapshot_version,g.published_at,
                g.expected_count,g.content_digest,g.owner,g.user_lease_token,
                g.user_lease_version,g.task_id,g.task_lease_token,g.task_lease_version,
                to_jsonb(t) as task_record,t.user_id as task_user_id,
                t.status as task_status,
                t.claimed_by as task_owner,t.lease_token as current_task_token,
                t.lease_version as current_task_version,
                t.user_lease_token as task_user_token,
                t.user_lease_version as task_user_version,
                a.task_id as audit_task_id,a.user_id as audit_user_id,
                a.lease_version as audit_task_version,
                a.ended_at as attempt_ended_at,a.end_reason as attempt_end_reason,
                a.worker_id as attempt_owner,a.lease_token as audit_task_token,
                a.user_lease_token as audit_user_token,
                a.user_lease_version as audit_user_version
                from vector_generations g
                left join vector_indexes i on i.tenant_id=g.tenant_id
                    and i.vector_kind=g.vector_kind and i.namespace=g.namespace
                    and i.index_key=g.index_key
                left join import_tasks t on t.id=g.task_id and t.user_id=g.tenant_id
                left join import_task_attempts a on a.task_id=g.task_id
                    and a.user_id=g.tenant_id and a.lease_version=g.task_lease_version
                where {selectors}''', parameters).fetchall()
        return tuple(next((row for row in rows if row['generation_id'] == generation_id
                           and (row['tenant_id'], row['vector_kind'], row['namespace'],
                                row['index_key']) == scope.key), None)
                     for scope, generation_id in requests)
