import tempfile
import unittest
from pathlib import Path

from hello_agents.memory.rag.pipeline import SimpleRAGPipeline


class PipelineMultiDocumentTests(unittest.TestCase):
    def make_pipeline(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        pipeline = SimpleRAGPipeline(
            cache_path=str(Path(tmpdir.name) / "rag-cache.json")
        )
        pipeline.dimension = 2
        pipeline._to_vector = (
            lambda text: [1.0, 0.0]
            if "alpha" in text.lower()
            else [0.0, 1.0]
        )
        return pipeline

    def test_search_filters_to_selected_document_ids(self):
        pipeline = self.make_pipeline()
        pipeline.add_text(
            "alpha only doc one",
            document_id="doc-1",
            metadata={"document_name": "One"},
        )
        pipeline.add_text(
            "alpha only doc two",
            document_id="doc-2",
            metadata={"document_name": "Two"},
        )
        pipeline.add_text(
            "alpha only doc three",
            document_id="doc-3",
            metadata={"document_name": "Three"},
        )

        results = pipeline.search("alpha", limit=10, document_ids=["doc-2", "doc-1"])

        self.assertEqual(
            {r["metadata"]["document_id"] for r in results},
            {"doc-1", "doc-2"},
        )
        self.assertNotIn(
            "doc-3",
            {r["metadata"]["document_id"] for r in results},
        )

    def test_search_keeps_legacy_document_id_behavior(self):
        pipeline = self.make_pipeline()
        pipeline.add_text("alpha doc one", document_id="doc-1")
        pipeline.add_text("alpha doc two", document_id="doc-2")

        results = pipeline.search("alpha", limit=10, document_id="doc-1")

        self.assertEqual([r["metadata"]["document_id"] for r in results], ["doc-1"])

    def test_search_rejects_empty_document_ids_without_expanding_to_all_docs(self):
        pipeline = self.make_pipeline()
        pipeline.add_text("alpha doc one", document_id="doc-1")

        with self.assertRaises(ValueError) as ctx:
            pipeline.search("alpha", limit=10, document_ids=[])

        self.assertIn("document_ids", str(ctx.exception))

    def test_source_dedupe_does_not_merge_different_unpaged_chunks(self):
        pipeline = self.make_pipeline()
        pipeline.add_text("alpha same prefix A" * 20, document_id="doc-1")
        pipeline.add_text(
            "alpha same prefix B" * 20,
            document_id="doc-1",
            replace_existing=False,
        )

        results = pipeline.search("alpha", limit=10, document_ids=["doc-1"])

        self.assertEqual(len(results), 2)

    def test_summary_sampling_uses_spare_slots_for_later_chunks(self):
        pipeline = self.make_pipeline()
        for page, contents in (
            (1, ["page one first", "page one later"]),
            (2, ["page two first", "page two later"]),
            (3, ["page three first", "page three later"]),
        ):
            for content in contents:
                pipeline.add_text(
                    content,
                    document_id="sample",
                    metadata={"page_number": page},
                    replace_existing=False,
                )
        pipeline.add_text("outside", document_id="other", metadata={"page_number": 1})

        self.assertEqual(
            [item["content"] for item in pipeline.get_document_summary_context("sample", 2)],
            ["page one first", "page three first"],
        )
        self.assertEqual(
            [item["content"] for item in pipeline.get_document_summary_context("sample", 5)],
            ["page one first", "page one later", "page two first",
             "page two later", "page three first"],
        )


if __name__ == "__main__":
    unittest.main()
