import os
import sqlite3
from typing import Dict, Any, List, Optional


class SQLiteDocumentStore:
    def __init__(self, database_path: str = "./memory_data/memory.db"):
        self.database_path = database_path
        os.makedirs(os.path.dirname(database_path), exist_ok=True)
        self.conn = sqlite3.connect(database_path, check_same_thread=False)
        self._init_tables()

    def _init_tables(self):
        cur = self.conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            content TEXT,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        self.conn.commit()

    def add_document(self, doc_id: str, content: str, metadata: str = "{}"):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO documents (id, content, metadata) VALUES (?, ?, ?)",
                (doc_id, content, metadata),
            )

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT id, content, metadata FROM documents WHERE id = ?", (doc_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "content": row[1], "metadata": row[2]}

    def delete_document(self, doc_id: str):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        self.conn.commit()

    def close(self) -> None:
        if getattr(self, "conn", None) is not None:
            self.conn.close()
            self.conn = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
