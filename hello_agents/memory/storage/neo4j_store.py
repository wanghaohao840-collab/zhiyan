from __future__ import annotations

import hashlib
import math
from threading import Lock
from typing import Any, Dict, Iterable, Optional

try:
    from neo4j import GraphDatabase, unit_of_work
except Exception:  # pragma: no cover - exercised when dependency is absent
    GraphDatabase = None
    unit_of_work = None


class Neo4jConfigError(ValueError):
    """Raised when the graph store cannot be configured safely."""


class Neo4jGraphStore:
    """Neo4j driver adapter for document-scoped graph persistence."""

    NODE_SPECS = {
        "chapters": ("Chapter", "chapter_id"),
        "chunks": ("Chunk", "chunk_id"),
        "concepts": ("Concept", "concept_id"),
        "knowledge_points": ("KnowledgePoint", "knowledge_point_id"),
        "persons": ("Person", "person_id"),
    }
    CANONICAL_ENTITY_COLLECTIONS = (
        ("concepts", "Concept", "concept_id"),
        ("knowledge_points", "KnowledgePoint", "knowledge_point_id"),
        ("persons", "Person", "person_id"),
    )
    RELATION_TYPES = frozenset(
        {
            "HAS_CHAPTER",
            "PARENT_OF",
            "HAS_CHUNK",
            "MENTIONS",
            "RELATED_TO",
            "PART_OF",
            "IS_A",
            "CONTRASTS_WITH",
            "DEPENDS_ON",
            "PREREQUISITE_OF",
        }
    )
    RELATION_TEMPLATES = {
        relation_type: (
            "/* GRAPH_WRITE_RELATION */ "
            "UNWIND $rows AS row "
            "MATCH (source {rag_namespace: $rag_namespace, "
            "document_id: $document_id, graph_id: row.source_id}) "
            "MATCH (target {rag_namespace: $rag_namespace, "
            "document_id: $document_id, graph_id: row.target_id}) "
            f"MERGE (source)-[r:{relation_type}]->(target) "
            "SET r += row.properties"
        )
        for relation_type in RELATION_TYPES
    }
    CONSTRAINTS = (
        "CREATE CONSTRAINT graph_document_scope IF NOT EXISTS "
        "FOR (n:Document) REQUIRE (n.rag_namespace, n.document_id) IS UNIQUE",
        "CREATE CONSTRAINT graph_chapter_id IF NOT EXISTS "
        "FOR (n:Chapter) REQUIRE "
        "(n.rag_namespace, n.document_id, n.chapter_id) IS UNIQUE",
        "CREATE CONSTRAINT graph_chunk_id IF NOT EXISTS "
        "FOR (n:Chunk) REQUIRE "
        "(n.rag_namespace, n.document_id, n.chunk_id) IS UNIQUE",
        "CREATE CONSTRAINT graph_concept_id IF NOT EXISTS "
        "FOR (n:Concept) REQUIRE "
        "(n.rag_namespace, n.document_id, n.concept_id) IS UNIQUE",
        "CREATE CONSTRAINT graph_knowledge_point_id IF NOT EXISTS "
        "FOR (n:KnowledgePoint) REQUIRE "
        "(n.rag_namespace, n.document_id, n.knowledge_point_id) IS UNIQUE",
        "CREATE CONSTRAINT graph_person_id IF NOT EXISTS "
        "FOR (n:Person) REQUIRE "
        "(n.rag_namespace, n.document_id, n.person_id) IS UNIQUE",
        "CREATE INDEX graph_concept_scope IF NOT EXISTS "
        "FOR (n:Concept) ON "
        "(n.rag_namespace, n.document_id, n.normalized_name)",
        "CREATE INDEX graph_knowledge_scope IF NOT EXISTS "
        "FOR (n:KnowledgePoint) ON "
        "(n.rag_namespace, n.document_id, n.normalized_name)",
        "CREATE INDEX graph_person_scope IF NOT EXISTS "
        "FOR (n:Person) ON "
        "(n.rag_namespace, n.document_id, n.normalized_name)",
        "CREATE CONSTRAINT graph_canonical_entity IF NOT EXISTS "
        "FOR (n:CanonicalEntity) REQUIRE "
        "(n.rag_namespace, n.entity_type, n.normalized_name) IS UNIQUE",
    )

    def __init__(
        self,
        *,
        driver: Any = None,
        database: str = "neo4j",
        uri: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        connection_timeout: float | None = None,
    ):
        database = str(database or "").strip()
        if not database:
            raise Neo4jConfigError("Neo4j database cannot be empty")

        if driver is None:
            if not uri or not username or not password:
                raise Neo4jConfigError(
                    "NEO4J_URI, NEO4J_USERNAME and NEO4J_PASSWORD are required"
                )
            if GraphDatabase is None:
                raise Neo4jConfigError(
                    "The neo4j package is required for graph storage"
                )
            options = {} if connection_timeout is None else {
                "connection_timeout": connection_timeout,
                "connection_acquisition_timeout": connection_timeout,
                "max_transaction_retry_time": 0,
            }
            created_driver = None
            try:
                created_driver = GraphDatabase.driver(
                    uri,
                    auth=(username, password),
                    **options,
                )
                created_driver.verify_connectivity()
            except Exception as error:
                if created_driver is not None:
                    try:
                        created_driver.close()
                    except Exception:
                        pass
                raise Neo4jConfigError(
                    f"Neo4j connection failed: {error.__class__.__name__}"
                ) from error
            driver = created_driver

        self._driver = driver
        self.database = database
        self._schema_initialized = False
        self._schema_lock = Lock()

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(database={self.database!r}, "
            f"schema_initialized={self._schema_initialized!r})"
        )

    __str__ = __repr__

    def close(self) -> None:
        self._driver.close()

    def initialize_schema(self) -> None:
        if self._schema_initialized:
            return
        with self._schema_lock:
            if self._schema_initialized:
                return
            with self._driver.session(database=self.database) as session:
                for query in self.CONSTRAINTS:
                    session.run(query)
            self._schema_initialized = True

    @staticmethod
    def _require_document_id(document_id: str) -> str:
        value = str(document_id or "").strip()
        if not value:
            raise ValueError("document_id is required")
        return value

    @staticmethod
    def _require_namespace(rag_namespace: str) -> str:
        value = str(rag_namespace or "").strip()
        if not value:
            raise ValueError("rag_namespace is required")
        return value

    @staticmethod
    def _cursor(value: Optional[str]) -> int:
        if value in (None, ""):
            return 0
        try:
            cursor = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError("cursor must be a non-negative integer") from error
        if cursor < 0:
            raise ValueError("cursor must be a non-negative integer")
        return cursor

    @staticmethod
    def _limit(value: int) -> int:
        limit = int(value)
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return limit

    @staticmethod
    def _canonical_id(
        rag_namespace: str,
        entity_type: str,
        normalized_name: str,
    ) -> str:
        digest = hashlib.sha256(
            f"{rag_namespace}\0{entity_type}\0{normalized_name}".encode(
                "utf-8"
            )
        ).hexdigest()
        return digest[:24]

    @staticmethod
    def _records(result: Any) -> list[dict[str, Any]]:
        if hasattr(result, "data"):
            return [dict(row) for row in result.data()]
        return [dict(row) for row in result]

    def replace_document_graph(
        self,
        document_id: str,
        build_id: str,
        graph: Dict[str, Any],
        *,
        rag_namespace: str = "default",
    ) -> Dict[str, int]:
        document_id = self._require_document_id(document_id)
        rag_namespace = self._require_namespace(rag_namespace)
        build_id = str(build_id or "").strip()
        if not build_id:
            raise ValueError("build_id is required")
        self.initialize_schema()

        def write_graph(tx):
            tx.run(
                "/* GRAPH_REPLACE_DELETE */ "
                "MATCH (n {rag_namespace: $rag_namespace, "
                "document_id: $document_id}) DETACH DELETE n",
                document_id=document_id,
                rag_namespace=rag_namespace,
            )
            document = dict(graph.get("document") or {})
            document.update(
                document_id=document_id,
                rag_namespace=rag_namespace,
                graph_id=document_id,
                build_id=build_id,
                graph_status="ready",
            )
            tx.run(
                "/* GRAPH_WRITE_DOCUMENT */ "
                "MERGE (d:Document {rag_namespace: $rag_namespace, "
                "document_id: $document_id}) "
                "SET d = $properties",
                document_id=document_id,
                rag_namespace=rag_namespace,
                properties=document,
            )

            node_count = 1
            for collection, (label, id_key) in self.NODE_SPECS.items():
                rows = []
                for value in graph.get(collection, []) or []:
                    properties = dict(value)
                    graph_id = str(properties.get(id_key) or "").strip()
                    if not graph_id:
                        raise ValueError(f"{id_key} is required")
                    properties.update(
                        document_id=document_id,
                        rag_namespace=rag_namespace,
                        graph_id=graph_id,
                    )
                    rows.append(properties)
                if not rows:
                    continue
                tx.run(
                    "/* GRAPH_WRITE_NODES */ "
                    "UNWIND $rows AS row "
                    f"MERGE (n:{label} {{rag_namespace: $rag_namespace, "
                    f"document_id: $document_id, {id_key}: row.{id_key}}}) "
                    "SET n = row",
                    document_id=document_id,
                    rag_namespace=rag_namespace,
                    rows=rows,
                )
                node_count += len(rows)

            structural = self._structural_relations(document_id, graph)
            supplied = list(graph.get("relations", []) or [])
            relation_rows: dict[str, list[dict[str, Any]]] = {}
            for relation in [*structural, *supplied]:
                relation_type = str(relation.get("type") or "").upper()
                if relation_type not in self.RELATION_TYPES:
                    raise ValueError(
                        f"unsupported graph relation type: {relation_type}"
                    )
                source_id = str(relation.get("source_id") or "").strip()
                target_id = str(relation.get("target_id") or "").strip()
                if not source_id or not target_id:
                    raise ValueError("relation endpoints are required")
                relation_rows.setdefault(relation_type, []).append(
                    {
                        "source_id": source_id,
                        "target_id": target_id,
                        "properties": dict(relation.get("properties") or {}),
                    }
                )
            for relation_type, rows in relation_rows.items():
                tx.run(
                    self.RELATION_TEMPLATES[relation_type],
                    document_id=document_id,
                    rag_namespace=rag_namespace,
                    rows=rows,
                )

            canonical_rows = []
            for (
                collection,
                entity_type,
                id_key,
            ) in self.CANONICAL_ENTITY_COLLECTIONS:
                for value in graph.get(collection, []) or []:
                    properties = dict(value)
                    normalized_name = str(
                        properties.get("normalized_name") or ""
                    ).strip().lower()
                    graph_id = str(properties.get(id_key) or "").strip()
                    if not normalized_name or not graph_id:
                        continue
                    canonical_rows.append(
                        {
                            "graph_id": graph_id,
                            "entity_type": entity_type,
                            "normalized_name": normalized_name,
                            "name": str(
                                properties.get("name")
                                or properties.get("title")
                                or normalized_name
                            ).strip(),
                            "canonical_id": self._canonical_id(
                                rag_namespace,
                                entity_type,
                                normalized_name,
                            ),
                        }
                    )
            if canonical_rows:
                tx.run(
                    "/* GRAPH_LINK_CANONICAL */ "
                    "UNWIND $rows AS row "
                    "MATCH (local {rag_namespace: $rag_namespace, "
                    "document_id: $document_id, graph_id: row.graph_id}) "
                    "WHERE row.entity_type IN labels(local) "
                    "MERGE (canonical:CanonicalEntity {"
                    "rag_namespace: $rag_namespace, "
                    "entity_type: row.entity_type, "
                    "normalized_name: row.normalized_name}) "
                    "ON CREATE SET canonical.canonical_id = row.canonical_id "
                    "SET canonical.name = coalesce(canonical.name, row.name) "
                    "MERGE (local)-[:REFERS_TO]->(canonical)",
                    document_id=document_id,
                    rag_namespace=rag_namespace,
                    rows=canonical_rows,
                )
            tx.run(
                "/* GRAPH_CLEAN_CANONICAL */ "
                "MATCH (canonical:CanonicalEntity {"
                "rag_namespace: $rag_namespace}) "
                "WHERE NOT ()-[:REFERS_TO]->(canonical) "
                "DELETE canonical",
                rag_namespace=rag_namespace,
            )

            tx.run(
                "/* GRAPH_MARK_READY */ "
                "MATCH (d:Document {rag_namespace: $rag_namespace, "
                "document_id: $document_id}) "
                "SET d.build_id = $build_id, d.graph_status = 'ready', "
                "d.updated_at = $updated_at",
                document_id=document_id,
                rag_namespace=rag_namespace,
                build_id=build_id,
                updated_at=document.get("updated_at"),
            )
            return {
                "node_count": node_count,
                "relation_count": sum(len(rows) for rows in relation_rows.values()),
            }

        with self._driver.session(database=self.database) as session:
            return dict(session.execute_write(write_graph))

    @staticmethod
    def _structural_relations(
        document_id: str,
        graph: Dict[str, Any],
    ) -> list[dict[str, Any]]:
        relations: list[dict[str, Any]] = []
        for chapter in graph.get("chapters", []) or []:
            chapter_id = chapter["chapter_id"]
            parent_id = chapter.get("parent_id")
            relations.append(
                {
                    "source_id": parent_id or document_id,
                    "target_id": chapter_id,
                    "type": "PARENT_OF" if parent_id else "HAS_CHAPTER",
                }
            )
        for chunk in graph.get("chunks", []) or []:
            relations.append(
                {
                    "source_id": chunk.get("chapter_id") or document_id,
                    "target_id": chunk["chunk_id"],
                    "type": "HAS_CHUNK",
                }
            )
        return relations

    def get_document_build(
        self,
        document_id: str,
        *,
        rag_namespace: str = "default",
    ) -> Optional[Dict[str, Any]]:
        document_id = self._require_document_id(document_id)
        rag_namespace = self._require_namespace(rag_namespace)

        def read(tx):
            result = tx.run(
                "/* GRAPH_GET_BUILD */ "
                "MATCH (d:Document {rag_namespace: $rag_namespace, "
                "document_id: $document_id}) "
                "RETURN d.build_id AS build_id, "
                "d.graph_status AS graph_status",
                document_id=document_id,
                rag_namespace=rag_namespace,
            )
            record = result.single()
            return dict(record) if record else None

        with self._driver.session(database=self.database) as session:
            return session.execute_read(read)

    def get_document_graph(
        self,
        document_id: str,
        *,
        rag_namespace: str = "default",
        node_cursor: Optional[str] = None,
        relation_cursor: Optional[str] = None,
        node_limit: int = 100,
        relation_limit: int = 100,
        include_chunk_content: bool = False,
    ) -> Dict[str, Any]:
        document_id = self._require_document_id(document_id)
        rag_namespace = self._require_namespace(rag_namespace)
        node_skip = self._cursor(node_cursor)
        relation_skip = self._cursor(relation_cursor)
        node_limit = self._limit(node_limit)
        relation_limit = self._limit(relation_limit)

        def read(tx):
            nodes = self._records(
                tx.run(
                    "/* GRAPH_QUERY_NODES */ "
                    "MATCH (n {rag_namespace: $rag_namespace, "
                    "document_id: $document_id}) "
                    "WITH n, labels(n)[0] AS node_type "
                    "RETURN n.graph_id AS id, node_type AS type, "
                    "properties(n) AS properties "
                    "ORDER BY type, id SKIP $skip LIMIT $limit",
                    document_id=document_id,
                    rag_namespace=rag_namespace,
                    skip=node_skip,
                    limit=node_limit + 1,
                )
            )
            relations = self._records(
                tx.run(
                    "/* GRAPH_QUERY_RELATIONS */ "
                    "MATCH (source {rag_namespace: $rag_namespace, "
                    "document_id: $document_id})-[r]->"
                    "(target {rag_namespace: $rag_namespace, "
                    "document_id: $document_id}) "
                    "RETURN source.graph_id AS source_id, "
                    "target.graph_id AS target_id, type(r) AS type, "
                    "properties(r) AS properties, "
                    "{id: source.graph_id, type: labels(source)[0], "
                    "name: source.name} AS source, "
                    "{id: target.graph_id, type: labels(target)[0], "
                    "name: target.name} AS target "
                    "ORDER BY type, source_id, target_id "
                    "SKIP $skip LIMIT $limit",
                    document_id=document_id,
                    rag_namespace=rag_namespace,
                    skip=relation_skip,
                    limit=relation_limit + 1,
                )
            )
            return nodes, relations

        with self._driver.session(database=self.database) as session:
            nodes, relations = session.execute_read(read)

        has_more_nodes = len(nodes) > node_limit
        has_more_relations = len(relations) > relation_limit
        nodes = nodes[:node_limit]
        relations = relations[:relation_limit]
        if not include_chunk_content:
            for node in nodes:
                if node.get("type") == "Chunk":
                    properties = dict(node.get("properties") or {})
                    properties.pop("content", None)
                    node["properties"] = properties
        return {
            "nodes": nodes,
            "relations": relations,
            "page": {
                "node_limit": node_limit,
                "relation_limit": relation_limit,
                "next_node_cursor": (
                    str(node_skip + len(nodes)) if has_more_nodes else None
                ),
                "next_relation_cursor": (
                    str(relation_skip + len(relations))
                    if has_more_relations
                    else None
                ),
            },
        }

    def check_database(self) -> None:
        with self._driver.session(database=self.database) as session:
            session.run("RETURN 1").single()

    def get_subgraph(
        self,
        *,
        rag_namespace: str,
        document_ids: list[str],
        query: str = "",
        limit: int = 120,
    ) -> dict[str, Any]:
        """Project only graph nodes and edges in an explicitly authorized scope."""
        rag_namespace = self._require_namespace(rag_namespace)
        if not 1 <= limit <= 200:
            raise ValueError("subgraph limit must be between 1 and 200")
        if not document_ids:
            return {"nodes": [], "edges": [], "truncated": False}

        node_query = """
            /* GRAPH_CANVAS_NODES */
            CALL {
                MATCH (n {rag_namespace: $rag_namespace})
                WHERE n.document_id IN $document_ids AND n.graph_id IS NOT NULL
                  AND labels(n)[0] IN $node_types
                RETURN n, [n.document_id] AS document_ids, labels(n)[0] AS node_type,
                       coalesce(n.name, n.title, n.graph_id) AS label,
                       n.graph_id AS graph_id, n.document_id AS scope_id
                UNION ALL
                MATCH (local {rag_namespace: $rag_namespace})-[r:REFERS_TO]->
                      (n:CanonicalEntity {rag_namespace: $rag_namespace})
                WHERE local.document_id IN $document_ids
                  AND n.canonical_id IS NOT NULL
                  AND labels(local)[0] = n.entity_type
                  AND coalesce(r.rag_namespace, $rag_namespace) = $rag_namespace
                  AND coalesce(r.document_id, local.document_id) = local.document_id
                WITH n, collect(DISTINCT local.document_id) AS document_ids,
                     min(coalesce(local.name, local.title, local.graph_id)) AS label
                RETURN n, document_ids, 'CanonicalEntity' AS node_type,
                       label, n.canonical_id AS graph_id, '' AS scope_id
            }
            WITH n, document_ids, node_type, label, graph_id, scope_id
            WHERE $search_term = '' OR toLower(label) CONTAINS $search_term
            RETURN elementId(n) AS element_id, document_ids, node_type AS type,
                   left(label, 300) AS label, graph_id, scope_id,
                   toFloatOrNull(n.weight) AS weight
            ORDER BY type, scope_id, graph_id
            LIMIT $limit
        """
        edge_query = """
            /* GRAPH_CANVAS_EDGES */
            UNWIND $element_ids AS selected_id
            MATCH (source {rag_namespace: $rag_namespace})-[r]->
                  (target {rag_namespace: $rag_namespace})
            WHERE elementId(source) = selected_id
              AND elementId(target) IN $element_ids
              AND coalesce(r.rag_namespace, $rag_namespace) = $rag_namespace
              AND coalesce(r.document_id, source.document_id) = source.document_id
              AND (
                  (type(r) IN $relation_types AND source.document_id = target.document_id)
                  OR (type(r) = 'REFERS_TO' AND target:CanonicalEntity
                      AND labels(source)[0] = target.entity_type)
              )
            RETURN elementId(source) AS source, elementId(target) AS target,
                   type(r) AS relation, coalesce(toFloatOrNull(r.weight),
                   toFloatOrNull(r.confidence)) AS weight
            ORDER BY relation, source, target
            LIMIT $limit
        """

        def read(tx):
            nodes = self._records(tx.run(
                node_query,
                rag_namespace=rag_namespace,
                document_ids=document_ids,
                node_types=["Document", *(label for label, _ in self.NODE_SPECS.values())],
                search_term=query.strip().lower(),
                limit=limit + 1,
            ))
            node_ids = [node["element_id"] for node in nodes[:limit]]
            edges = self._records(tx.run(
                edge_query,
                rag_namespace=rag_namespace,
                element_ids=node_ids,
                relation_types=sorted(self.RELATION_TYPES),
                limit=limit + 1,
            )) if node_ids else []
            return nodes, edges

        if unit_of_work is not None:
            read = unit_of_work(timeout=5)(read)
        with self._driver.session(database=self.database) as session:
            raw_nodes, raw_edges = session.execute_read(read)
        nodes = []
        id_map = {}
        for node in raw_nodes[:limit]:
            # graph_id is stable across a document rebuild. elementId only
            # joins the two database projections within this read transaction.
            node_id = f"{node['type']}:{node['scope_id']}:{node['graph_id']}"
            id_map[node["element_id"]] = node_id
            nodes.append({
                "id": node_id,
                "label": node["label"],
                "type": node["type"],
                "document_ids": sorted(node["document_ids"]),
                "weight": self._canvas_weight(node["weight"]),
            })
        edges = [
            {
                "source": id_map[edge["source"]],
                "target": id_map[edge["target"]],
                "relation": edge["relation"],
                "weight": self._canvas_weight(edge["weight"]),
            }
            for edge in raw_edges[:limit]
        ]
        return {
            "nodes": nodes,
            "edges": edges,
            "truncated": len(raw_nodes) > limit or len(raw_edges) > limit,
        }

    @staticmethod
    def _canvas_weight(value: float | None) -> float:
        return value if value is not None and math.isfinite(value) and value >= 0 else 1.0

    def get_graph_context(
        self,
        document_id: str,
        *,
        query_terms: Iterable[str],
        rag_namespace: str = "default",
        node_limit: int = 8,
        relation_limit: int = 16,
    ) -> Dict[str, Any]:
        """Return bounded one-hop graph context for lexical seed terms."""

        document_id = self._require_document_id(document_id)
        rag_namespace = self._require_namespace(rag_namespace)
        node_limit = self._limit(node_limit)
        relation_limit = self._limit(relation_limit)
        terms = []
        for value in query_terms:
            term = str(value or "").strip().lower()
            if term and term not in terms:
                terms.append(term)
        if not terms:
            return {"entities": [], "relations": []}

        query = (
            "/* GRAPH_QUERY_CONTEXT */ "
            "CALL { "
            "MATCH (seed {rag_namespace: $rag_namespace, "
            "document_id: $document_id}) "
            "WHERE any(label IN labels(seed) WHERE label IN $seed_labels) "
            "AND any(term IN $query_terms WHERE "
            "toLower(coalesce(seed.normalized_name, '')) CONTAINS term "
            "OR toLower(coalesce(seed.name, '')) CONTAINS term "
            "OR toLower(coalesce(seed.title, '')) CONTAINS term "
            "OR toLower(coalesce(seed.description, '')) CONTAINS term) "
            "WITH seed ORDER BY seed.graph_id LIMIT $node_limit "
            "OPTIONAL MATCH (seed)-[r]-(neighbor "
            "{rag_namespace: $rag_namespace, document_id: $document_id}) "
            "RETURN seed, r, neighbor "
            "LIMIT $row_limit "
            "} "
            "RETURN {id: seed.graph_id, type: labels(seed)[0], "
            "name: coalesce(seed.name, seed.title), "
            "properties: properties(seed)} AS entity, "
            "CASE WHEN neighbor IS NULL THEN NULL ELSE "
            "{id: neighbor.graph_id, type: labels(neighbor)[0], "
            "name: coalesce(neighbor.name, neighbor.title), "
            "properties: properties(neighbor)} END AS neighbor, "
            "CASE WHEN r IS NULL THEN NULL ELSE "
            "{source_id: startNode(r).graph_id, "
            "target_id: endNode(r).graph_id, type: type(r), "
            "properties: properties(r)} END AS relation "
            "ORDER BY entity.id, relation.type, neighbor.id"
        )
        row_limit = min(5000, node_limit * max(relation_limit, 1))
        with self._driver.session(database=self.database) as session:
            rows = session.execute_read(
                lambda tx: self._records(
                    tx.run(
                        query,
                        document_id=document_id,
                        rag_namespace=rag_namespace,
                        query_terms=terms,
                        seed_labels=[
                            "Concept",
                            "KnowledgePoint",
                            "Person",
                            "Chapter",
                        ],
                        node_limit=node_limit,
                        relation_limit=relation_limit,
                        row_limit=row_limit,
                    )
                )
            )

        def clean_properties(value: Any) -> dict[str, Any]:
            properties = dict(value or {})
            properties.pop("content", None)
            return properties

        entities: dict[str, dict[str, Any]] = {}
        relations: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            for key in ("entity", "neighbor"):
                item = row.get(key)
                if not item or not item.get("id"):
                    continue
                entity = dict(item)
                entity["properties"] = clean_properties(entity.get("properties"))
                entities.setdefault(str(entity["id"]), entity)
            relation = row.get("relation")
            if not relation:
                continue
            relation = dict(relation)
            relation["properties"] = clean_properties(
                relation.get("properties")
            )
            relation_key = (
                str(relation.get("source_id") or ""),
                str(relation.get("target_id") or ""),
                str(relation.get("type") or ""),
            )
            if relation_key[0] and relation_key[1]:
                relations.setdefault(relation_key, relation)
            if len(relations) >= relation_limit:
                break
        return {
            "entities": list(entities.values())[:node_limit],
            "relations": list(relations.values())[:relation_limit],
        }

    def get_cross_document_entities(
        self,
        document_ids: Iterable[str],
        *,
        query_terms: Iterable[str],
        rag_namespace: str = "default",
        entity_limit: int = 12,
        evidence_limit: int = 40,
    ) -> Dict[str, Any]:
        """Return bounded exact-name entities shared by selected documents."""

        rag_namespace = self._require_namespace(rag_namespace)
        selected_ids = []
        for value in document_ids:
            document_id = str(value or "").strip()
            if not document_id:
                raise ValueError("document_id is required")
            if document_id not in selected_ids:
                selected_ids.append(document_id)
        if not 2 <= len(selected_ids) <= 10:
            raise ValueError("document_ids must contain between 2 and 10 items")
        entity_limit = self._limit(entity_limit)
        evidence_limit = self._limit(evidence_limit)
        terms = []
        for value in query_terms:
            term = str(value or "").strip().lower()
            if term and term not in terms:
                terms.append(term)
        if not terms:
            return {"entities": []}

        query = (
            "/* GRAPH_QUERY_CROSS_DOCUMENT_ENTITIES */ "
            "MATCH (local) "
            "WHERE local.rag_namespace = $rag_namespace "
            "AND local.document_id IN $document_ids "
            "AND any(label IN labels(local) WHERE label IN $entity_labels) "
            "AND trim(coalesce(local.normalized_name, '')) <> '' "
            "AND any(term IN $query_terms WHERE "
            "toLower(coalesce(local.normalized_name, '')) CONTAINS term "
            "OR toLower(coalesce(local.name, '')) CONTAINS term "
            "OR toLower(coalesce(local.title, '')) CONTAINS term) "
            "WITH local ORDER BY local.document_id, local.graph_id "
            "WITH head([label IN labels(local) WHERE label IN $entity_labels]) "
            "AS entity_type, toLower(trim(local.normalized_name)) "
            "AS normalized_name, collect(local) AS members "
            "WITH entity_type, normalized_name, members, "
            "reduce(ids = [], member IN members | "
            "CASE WHEN member.document_id IN ids THEN ids "
            "ELSE ids + member.document_id END) AS document_ids "
            "WHERE size(document_ids) >= 2 "
            "OPTIONAL MATCH (canonical:CanonicalEntity {"
            "rag_namespace: $rag_namespace, entity_type: entity_type, "
            "normalized_name: normalized_name}) "
            "RETURN coalesce(canonical.canonical_id, "
            "entity_type + ':' + normalized_name) AS canonical_id, "
            "entity_type, normalized_name, "
            "coalesce(canonical.name, members[0].name, "
            "members[0].title, normalized_name) AS name, "
            "[member IN members[..$evidence_limit] | {"
            "document_id: member.document_id, id: member.graph_id, "
            "type: entity_type, name: coalesce(member.name, member.title), "
            "properties: properties(member)}] AS members "
            "ORDER BY entity_type, normalized_name "
            "LIMIT $entity_limit"
        )
        with self._driver.session(database=self.database) as session:
            rows = session.execute_read(
                lambda tx: self._records(
                    tx.run(
                        query,
                        document_ids=selected_ids,
                        rag_namespace=rag_namespace,
                        query_terms=terms,
                        entity_labels=[
                            "Concept",
                            "KnowledgePoint",
                            "Person",
                        ],
                        entity_limit=entity_limit,
                        evidence_limit=evidence_limit,
                    )
                )
            )

        entities = []
        for row in rows[:entity_limit]:
            entity = dict(row)
            members = []
            for member in entity.get("members") or []:
                clean_member = dict(member)
                properties = dict(clean_member.get("properties") or {})
                properties.pop("content", None)
                clean_member["properties"] = properties
                members.append(clean_member)
            members.sort(
                key=lambda item: (
                    str(item.get("document_id") or ""),
                    str(item.get("id") or ""),
                )
            )
            entity["members"] = members[:evidence_limit]
            entities.append(entity)
        entities.sort(
            key=lambda item: (
                str(item.get("entity_type") or ""),
                str(item.get("normalized_name") or ""),
            )
        )
        return {"entities": entities[:entity_limit]}

    def get_chapters(
        self,
        document_id: str,
        *,
        rag_namespace: str = "default",
    ) -> list[dict[str, Any]]:
        document_id = self._require_document_id(document_id)
        rag_namespace = self._require_namespace(rag_namespace)
        with self._driver.session(database=self.database) as session:
            return session.execute_read(
                lambda tx: self._records(
                    tx.run(
                        "/* GRAPH_QUERY_CHAPTERS */ "
                        "MATCH (c:Chapter {rag_namespace: $rag_namespace, "
                        "document_id: $document_id}) "
                        "OPTIONAL MATCH (c)-[:HAS_CHUNK]->(chunk:Chunk) "
                        "RETURN c.chapter_id AS chapter_id, c.title AS title, "
                        "c.level AS level, c.order AS order, "
                        "c.heading_path AS heading_path, "
                        "c.parent_id AS parent_id, "
                        "collect(chunk.chunk_id) AS chunk_ids "
                        "ORDER BY order LIMIT 2001",
                        document_id=document_id,
                        rag_namespace=rag_namespace,
                    )
                )
            )

    def get_typed_relations(
        self,
        document_id: str,
        *,
        rag_namespace: str = "default",
        node_label: str,
        relation_types: Iterable[str],
        name: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        document_id = self._require_document_id(document_id)
        rag_namespace = self._require_namespace(rag_namespace)
        allowed_labels = {"Concept", "KnowledgePoint", "Person"}
        if node_label not in allowed_labels:
            raise ValueError("unsupported node label")
        selected_types = sorted(
            {
                str(value).upper()
                for value in relation_types
                if str(value).upper() in self.RELATION_TYPES
            }
        )
        if not selected_types:
            raise ValueError("at least one supported relation type is required")
        skip = self._cursor(cursor)
        limit = self._limit(limit)

        query = (
            "/* GRAPH_QUERY_TYPED_RELATIONS */ "
            f"MATCH (source:{node_label} {{rag_namespace: $rag_namespace, "
            f"document_id: $document_id}})-[r]->"
            f"(target:{node_label} {{rag_namespace: $rag_namespace, "
            f"document_id: $document_id}}) "
            "WHERE type(r) IN $relation_types "
            "AND ($name IS NULL OR source.normalized_name = $name "
            "OR target.normalized_name = $name) "
            "RETURN source.graph_id AS source_id, "
            "target.graph_id AS target_id, type(r) AS type, "
            "properties(r) AS properties, "
            "{id: source.graph_id, type: labels(source)[0], "
            "name: source.name} AS source, "
            "{id: target.graph_id, type: labels(target)[0], "
            "name: target.name} AS target "
            "ORDER BY type, source_id, target_id SKIP $skip LIMIT $limit"
        )
        with self._driver.session(database=self.database) as session:
            rows = session.execute_read(
                lambda tx: self._records(
                    tx.run(
                        query,
                        document_id=document_id,
                        rag_namespace=rag_namespace,
                        relation_types=selected_types,
                        name=name,
                        skip=skip,
                        limit=limit + 1,
                    )
                )
            )
        has_more = len(rows) > limit
        rows = rows[:limit]
        nodes: dict[str, dict[str, Any]] = {}
        for row in rows:
            nodes[row["source"]["id"]] = dict(row["source"])
            nodes[row["target"]["id"]] = dict(row["target"])
        return {
            "nodes": list(nodes.values()),
            "relations": rows,
            "page": {
                "limit": limit,
                "next_cursor": str(skip + len(rows)) if has_more else None,
            },
        }

    def delete_document(
        self,
        document_id: str,
        *,
        rag_namespace: str = "default",
    ) -> Dict[str, int]:
        document_id = self._require_document_id(document_id)
        rag_namespace = self._require_namespace(rag_namespace)

        def delete(tx):
            result = tx.run(
                "/* GRAPH_DELETE_COUNT */ "
                "MATCH (n {rag_namespace: $rag_namespace, "
                "document_id: $document_id}) "
                "OPTIONAL MATCH (n)-[r]-() "
                "RETURN count(DISTINCT n) AS nodes_removed, "
                "count(DISTINCT r) AS relations_removed",
                document_id=document_id,
                rag_namespace=rag_namespace,
            )
            record = result.single() or {
                "nodes_removed": 0,
                "relations_removed": 0,
            }
            counts = {
                "nodes_removed": int(record.get("nodes_removed", 0)),
                "relations_removed": int(record.get("relations_removed", 0)),
            }
            tx.run(
                "/* GRAPH_DELETE */ "
                "MATCH (n {rag_namespace: $rag_namespace, "
                "document_id: $document_id}) DETACH DELETE n",
                document_id=document_id,
                rag_namespace=rag_namespace,
            )
            tx.run(
                "/* GRAPH_CLEAN_CANONICAL */ "
                "MATCH (canonical:CanonicalEntity {"
                "rag_namespace: $rag_namespace}) "
                "WHERE NOT ()-[:REFERS_TO]->(canonical) "
                "DELETE canonical",
                rag_namespace=rag_namespace,
            )
            return counts

        if unit_of_work is not None:
            delete = unit_of_work(timeout=30)(delete)
        with self._driver.session(database=self.database) as session:
            return session.execute_write(delete)
