import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.config import get_settings
from app.runbook_knowledge import RunbookKnowledge


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = get_settings(_env_file=None, data_dir=Path(self.temp.name))
        self.knowledge = RunbookKnowledge(cfg.runbooks_dir, settings=cfg)
        self.records = [dict(id="a", text="DNS failed", environment="any", family="dns"),
                        dict(id="b", text="TLS expired", environment="any", family="tls")]

    @staticmethod
    def encode(texts):
        return [[1.0, 0.0] if "DNS" in t else [0.0, 1.0] for t in texts]

    def test_vector_recall_without_lexical_match_and_hybrid_fuses(self):
        with patch.object(self.knowledge.kb, "_encode", side_effect=self.encode):
            plain, _ = self.knowledge.search("other wording", self.records, strategy="bm25")
            dense, _ = self.knowledge.search("other wording", self.records, strategy="vector", limit=1)
            mixed, _ = self.knowledge.search("other wording", self.records, strategy="hybrid", limit=1)
        self.assertEqual(plain, [])
        self.assertEqual([r["id"] for r in dense], ["b"])
        self.assertEqual(dense, mixed)

    def test_metadata_changes_do_not_reencode_content_or_return_stale_metadata(self):
        with patch.object(self.knowledge.kb, "_encode", side_effect=self.encode) as encode:
            self.knowledge.search("DNS", self.records, strategy="hybrid")
            changed = copy.deepcopy(self.records)
            changed[0]["version"] = "new"
            rows, status = self.knowledge.search("DNS", changed, strategy="hybrid")
            self.assertEqual(encode.call_count, 2)  # document batch + query
        self.assertTrue(status["query_cache_hit"])
        self.assertEqual(status["documents_encoded"], 0)
        self.assertEqual(rows[0]["version"], "new")

    def test_changed_document_only_encodes_delta_and_old_snapshot_remains_usable(self):
        with patch.object(self.knowledge.kb, "_encode", side_effect=self.encode) as encode:
            before, _ = self.knowledge.search("DNS", self.records, strategy="vector")
            changed = copy.deepcopy(self.records)
            changed[0]["text"] += " revised"
            _, status = self.knowledge.search("DNS", changed, strategy="vector")
            old, _ = self.knowledge.search("DNS", self.records, strategy="vector")
        self.assertEqual(status["documents_encoded"], 1)
        self.assertEqual(encode.call_count, 3)
        self.assertEqual(old, before)

    def test_scope_is_filtered_before_encoding_and_query_cannot_override_it(self):
        scoped = self.records + [dict(id="wrong", text="DNS private", environment="wrong", family="dns")]
        with patch.object(self.knowledge.kb, "_encode", side_effect=self.encode) as encode:
            rows, _ = self.knowledge.search("DNS environment wrong", scoped, strategy="hybrid", environments=["right"])
        self.assertNotIn("wrong", [r["id"] for r in rows])
        self.assertNotIn("DNS private", encode.call_args_list[0].args[0])

    def test_concurrent_search_only_embeds_shared_data_once(self):
        with patch.object(self.knowledge.kb, "_encode", side_effect=self.encode) as encode:
            with ThreadPoolExecutor(max_workers=4) as pool:
                rows = list(pool.map(lambda _: self.knowledge.search("DNS", self.records, strategy="hybrid"), range(8)))
        self.assertEqual(encode.call_count, 2)
        self.assertTrue(all(r[0] == rows[0][0] for r in rows))

    def test_malformed_vectors_are_not_cached_and_fallback_is_visible(self):
        with patch.object(self.knowledge.kb, "_encode", return_value=[[float("nan"), 0], [0, 1]]):
            rows, status = self.knowledge.search("DNS", self.records, strategy="vector")
        self.assertTrue(status["degraded"])
        self.assertEqual(status["strategy"], "bm25")
        self.assertEqual(status["requested_strategy"], "vector")
        self.assertEqual(rows[0]["id"], "a")
        self.assertFalse(self.knowledge._vectors)

    def test_cache_eviction_does_not_truncate_search_or_corrupt_snapshots(self):
        rows = [dict(id=str(i), text="DNS " + str(i)) for i in range(270)]
        with patch.object(self.knowledge.kb, "_encode", side_effect=self.encode):
            found, _ = self.knowledge.search("DNS", rows, strategy="vector", limit=270)
            again, _ = self.knowledge.search("DNS", rows, strategy="vector", limit=270)
        self.assertEqual(found, again)
        self.assertEqual(len(found), 270)
        self.assertEqual(len(self.knowledge._vectors), 256)

    def test_conflict_not_hidden_by_vector_top_one(self):
        rows = self.records + [dict(id="c", text="DNS new", environment="any", family="dns")]
        with patch.object(self.knowledge.kb, "_encode", side_effect=self.encode):
            result, _ = self.knowledge.search("DNS", rows, strategy="vector", limit=1)
        self.assertTrue(result[0]["conflicted"])

    def test_invalid_parameters_fail_explicitly(self):
        for options in ({"strategy": "typo"}, {"limit": -1}, {"limit": True}):
            with self.assertRaises(ValueError):
                self.knowledge.search("DNS", self.records, **options)

    def test_unknown_or_conflicting_environment_cannot_consume_slots_with_specific_docs(self):
        rows=self.records+[dict(id="specific",text="DNS",environment="managed",family="local")]
        for env in ([],["managed","other"]):
            with patch.object(self.knowledge.kb,"_encode",side_effect=self.encode):
                result,status=self.knowledge.search("DNS managed",rows,strategy="vector",environments=env)
            self.assertNotIn("specific",{r["id"] for r in result})
            self.assertEqual(status["excluded_environment_count"],1)

    def test_only_specific_documents_without_observed_environment_returns_explicit_gap(self):
        rows=[dict(id="specific",text="DNS",environment="managed",family="local")]
        result,status=self.knowledge.search("DNS managed",rows,strategy="vector")
        self.assertEqual(result,[])
        self.assertEqual(status["scope_status"],"unknown")
        self.assertEqual(status["reason"],"no_applicable_runbook")

    def test_bge_query_instruction_is_not_added_to_documents_and_changes_query_cache_identity(self):
        with patch.object(self.knowledge.kb,"_encode",side_effect=self.encode) as encode:
            self.knowledge.search("DNS",self.records,strategy="vector")
            self.assertEqual(encode.call_args_list[0].args[0],[r["text"] for r in self.records])
            self.assertEqual(encode.call_args_list[1].args[0],[self.knowledge.query_instruction+"DNS"])
            self.knowledge.query_instruction=""
            self.knowledge.search("DNS",self.records,strategy="vector")
        self.assertEqual(encode.call_count,3)

    def test_vector_mode_does_not_pay_for_unused_lexical_ranking(self):
        with patch.object(self.knowledge.kb,"_encode",side_effect=self.encode), \
             patch.object(self.knowledge,"_lexical_ranking",side_effect=AssertionError("not a fusion")):
            result,_=self.knowledge.search("DNS",self.records,strategy="vector")
        self.assertTrue(result)

    def test_investigation_loads_only_local_weights_and_missing_weights_degrade(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock
        factory=Mock(side_effect=OSError("weights not provisioned"))
        module=SimpleNamespace(SentenceTransformer=factory)
        with patch.dict(sys.modules,{"sentence_transformers":module}):
            rows,status=self.knowledge.search("DNS",self.records,strategy="vector")
        self.assertTrue(factory.call_args.kwargs["local_files_only"])
        self.assertTrue(status["degraded"])
        self.assertEqual(status["strategy"],"bm25")
        self.assertEqual(rows[0]["id"],"a")

    def test_exact_and_semantic_dedup_are_scope_aware(self):
        cfg = get_settings(_env_file=None, data_dir=Path(self.temp.name),
                           rag_rerank_mode="off", rag_semantic_dedup=True,
                           rag_semantic_dedup_threshold=.95)
        knowledge = RunbookKnowledge(cfg.runbooks_dir, settings=cfg)
        records = [
            {"id": "a", "text": "DNS query failed and the tunnel is established", "resource": "vpn",
             "environment": "any", "family": "vpn-dns", "version": "v1"},
            {"id": "a-copy", "text": " DNS query failed and the tunnel is established ", "resource": "vpn",
             "environment": "any", "family": "vpn-dns", "version": "v1"},
            {"id": "b", "text": "域名解析异常，但适用条件不同", "resource": "vpn",
             "environment": "managed-terminal", "family": "vpn-dns", "version": "v1"},
        ]
        def encode(texts):
            return [[1.0, 0.0] for _ in texts]
        with patch.object(knowledge.kb, "_encode", side_effect=encode):
            rows, status = knowledge.search("DNS", records, strategy="vector", limit=3,
                                            environments=["any"])
        self.assertEqual([row["id"] for row in rows], ["a"])
        self.assertEqual(status["exact_duplicates_removed"], 1)
        self.assertEqual(status["semantic_dedup"]["removed"], 0)

    def test_semantic_dedup_removes_near_duplicate_only_in_same_scope(self):
        cfg = get_settings(_env_file=None, data_dir=Path(self.temp.name),
                           rag_rerank_mode="off", rag_semantic_dedup=True,
                           rag_semantic_dedup_threshold=.95)
        knowledge = RunbookKnowledge(cfg.runbooks_dir, settings=cfg)
        records = [
            {"id": "a", "text": "DNS query failed", "resource": "vpn", "environment": "any",
             "family": "vpn-dns", "version": "v1"},
            {"id": "b", "text": "name resolution failed", "resource": "vpn", "environment": "any",
             "family": "vpn-dns", "version": "v1"},
            {"id": "c", "text": "name resolution failed", "resource": "vpn", "environment": "managed",
             "family": "vpn-dns", "version": "v1"},
        ]
        def encode(texts):
            return [[1.0, 0.0] for _ in texts]
        with patch.object(knowledge.kb, "_encode", side_effect=encode):
            rows, status = knowledge.search("DNS", records, strategy="vector", limit=3,
                                            environments=["managed"])
        self.assertEqual({row["id"] for row in rows}, {"a", "c"})
        self.assertEqual(status["semantic_dedup"]["removed"], 1)

    def test_cross_encoder_reranks_candidates_and_records_score(self):
        class FakeReranker:
            def predict(self, pairs):
                return [0.2 if "generic" in doc else 0.9 for _, doc in pairs]

        cfg = get_settings(_env_file=None, data_dir=Path(self.temp.name),
                           rag_rerank_mode="optional", rag_rerank_min_score=.0,
                           rag_semantic_dedup=False)
        knowledge = RunbookKnowledge(cfg.runbooks_dir, settings=cfg, reranker=FakeReranker())
        records = [
            {"id": "generic", "text": "generic DNS guidance", "resource": "vpn", "environment": "any",
             "family": "generic", "version": "v1"},
            {"id": "specific", "text": "specific DNS guidance", "resource": "vpn", "environment": "any",
             "family": "specific", "version": "v1"},
        ]
        rows, status = knowledge.search("DNS", records, strategy="bm25", limit=1)
        self.assertEqual(rows[0]["id"], "specific")
        self.assertEqual(rows[0]["rerank_score"], .9)
        self.assertTrue(status["rerank"]["applied"])
