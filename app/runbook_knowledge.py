"""Shared knowledge implementation, with immutable, resource-scoped runbook views."""
import hashlib
import math
import re
import time
from collections import Counter, OrderedDict

from .knowledge import KnowledgeBase


class RunbookKnowledge:
    def __init__(self, directory, *, settings, reranker=None):
        local = settings.kb_dir.parent / "models" / "bge-small-zh-v1.5"
        embedding = settings.embedding_model or (str(local) if (local / "config.json").is_file() else None)
        self.kb = KnowledgeBase(directory, settings=settings, purpose="runbook", embedding_model=embedding,
                                reranker=reranker)
        self.strategy = settings.runbook_strategy
        # BGE short-query / passage asymmetry. Do not add BGE instructions to
        # arbitrary user-configured embedding models or to source documents.
        self.query_instruction = ("为这个句子生成表示以用于检索相关文章："
            if self.kb.model_name.replace("\\", "/").rstrip("/").split("/")[-1] == "bge-small-zh-v1.5" else "")
        # Per-service caches contain derived data only. Sources always come from the
        # caller's immutable investigation snapshot, never from these caches.
        self._vectors = OrderedDict()
        self._queries = OrderedDict()

    @staticmethod
    def scope(records, environments=()):
        environments = set(environments)
        allowed = {"any"}
        if len(environments) == 1:
            allowed.update(environments)
        # A deployment-specific procedure cannot be applied without observed
        # deployment evidence. Unknown/conflicting markers retain generic docs.
        return [r for r in records if r.get("environment", "any") in allowed]

    @staticmethod
    def _remember(cache, key, value, capacity):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > capacity:
            cache.popitem(last=False)

    def _key(self, text):
        return hashlib.sha256((self.kb.model_name + "\0" + text).encode()).hexdigest()

    @staticmethod
    def _exact_records(records):
        selected, seen_ids, seen_content = [], set(), set()
        for record in records:
            ident = record.get("id")
            text = re.sub(r"\s+", " ", str(record.get("text", "")).strip().casefold())
            content_key = (text, tuple(record.get(key, "") for key in
                                       ("resource", "environment", "family", "version", "codes"))) if text else None
            if ident in seen_ids or (content_key and content_key in seen_content):
                continue
            if ident:
                seen_ids.add(ident)
            if content_key:
                seen_content.add(content_key)
            selected.append(record)
        return selected, len(records) - len(selected)

    def _cached_vectors(self, records):
        keys = [self._key(r["text"]) for r in records]
        with self.kb._lock:
            missing = {key: r["text"] for key, r in zip(keys, records) if key not in self._vectors}
            fresh = {}
            if missing:
                encoded = self.kb._encode(list(missing.values()))
                if len(encoded) != len(missing):
                    raise ValueError("embedding count mismatch")
                fresh = dict(zip(missing, encoded))
            vectors = [fresh[key] if key in fresh else self._vectors[key] for key in keys]
            if not vectors or any(not vector or any(not math.isfinite(x) for x in vector) for vector in vectors):
                raise ValueError("invalid document embeddings")
            if any(len(vector) != len(vectors[0]) for vector in vectors):
                raise ValueError("invalid embedding dimensions")
            for key, vector in zip(keys, vectors):
                self._remember(self._vectors, key, vector, 256)
        return vectors, {"documents_encoded": len(missing), "document_cache_hits": len(records)-len(missing)}

    def _dense_ranking(self, query, records):
        # Serializing lazy model load and inference also prevents duplicate loads
        # when VPN and ERP investigations start concurrently.
        with self.kb._lock:
            vectors, cache = self._cached_vectors(records)
            query = self.query_instruction + query
            query_key = self._key(query)
            query_hit = query_key in self._queries
            q = self._queries[query_key] if query_hit else self.kb._encode([query])[0]
            if not q or any(len(v) != len(q) for v in vectors) or any(
                    not math.isfinite(x) for vector in [q, *vectors] for x in vector):
                raise ValueError("invalid embedding dimensions or values")
            self._remember(self._queries, query_key, q, 64)
            scores = [sum(a*b for a, b in zip(q, vector)) for vector in vectors]
        return sorted(range(len(records)), key=lambda i: (-scores[i], records[i]["id"])), {
            **cache, "query_cache_hit": query_hit}

    def _semantic_deduplicate(self, records):
        if not self.kb.semantic_dedup_enabled or len(records) < 2:
            return records, {"enabled": self.kb.semantic_dedup_enabled, "applied": False,
                             "removed": 0, "reason": "not_needed"}
        try:
            vectors, cache = self._cached_vectors(records)
            keep, keep_vectors = [], []
            removed = 0
            for record, vector in zip(records, vectors):
                duplicate = False
                for prior, prior_vector in zip(keep, keep_vectors):
                    if not self.kb._has_applicability_metadata(record) or not self.kb._has_applicability_metadata(prior):
                        continue
                    if self.kb._scope_key(record) != self.kb._scope_key(prior):
                        continue
                    if sum(a * b for a, b in zip(vector, prior_vector)) >= self.kb.semantic_dedup_threshold:
                        duplicate = True
                        removed += 1
                        break
                if not duplicate:
                    keep.append(record)
                    keep_vectors.append(vector)
            return keep, {"enabled": True, "applied": True, "removed": removed,
                          "threshold": self.kb.semantic_dedup_threshold, **cache}
        except Exception as exc:
            return records, {"enabled": True, "applied": False, "removed": 0,
                             "degraded": True, "reason": type(exc).__name__}

    def capture(self, resource):
        with self.kb._lock:
            self.kb.documents = self.kb._load_documents()
            return [{**d, "text": d["text"]} for d in self.kb.documents if d["resource"] == resource]

    @staticmethod
    def conflicts(records):
        """Conflicts are declared scope/version collisions, not arbitrary semantic inference."""
        groups = {}
        for record in records:
            groups.setdefault((record.get("family", record["id"]), record.get("environment", "any")), []).append(record)
        return {r["id"] for group in groups.values() if len(group) > 1 for r in group}

    @staticmethod
    def _lexical_ranking(query, records):
        terms = set(KnowledgeBase._terms(query))
        counts = [Counter(KnowledgeBase._terms(r["text"])) for r in records]
        lengths = [sum(c.values()) for c in counts]
        avg = sum(lengths) / len(lengths) or 1
        freq = {t: sum(t in c for c in counts) for t in terms}
        scores = []
        for i, count in enumerate(counts):
            score = sum(math.log(1+(len(counts)-freq[t]+.5)/(freq[t]+.5))*count[t]*2.2 /
                        (count[t]+1.2*(.25+.75*lengths[i]/avg)) for t in terms)
            if score > 0:
                scores.append((score, i))
        return [i for _, i in sorted(scores, key=lambda x: (-x[0], records[x[1]]["id"]))]

    def search(self, query, records, limit=2, strategy=None, environments=()):
        start = time.perf_counter()
        strategy = strategy or self.strategy
        if strategy not in {"bm25", "vector", "hybrid"}:
            raise ValueError("unsupported runbook strategy")
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        # Only explicit backend observations supply this scope, never the generated query.
        environments = set(environments)
        total = len(records)
        records = self.scope(records, environments)
        records, exact_removed = self._exact_records(records)
        candidate_limit = max(limit, self.kb.rerank_candidate_k)
        status = {"strategy": strategy, "requested_strategy": strategy,
                  "profile": "applicable_hybrid_rerank_v1",
                  "observed_environments": sorted(environments), "candidate_count": len(records),
                  "scope_status": "confirmed" if len(environments)==1 else "conflicting" if environments else "unknown",
                  "excluded_environment_count": total-len(records),
                  "exact_duplicates_removed": exact_removed}
        if len(environments) == 1:
            environment = next(iter(environments))
            query = query + " " + environment
        if not records:
            status.update(elapsed_ms=round((time.perf_counter()-start)*1000, 3), reason="no_applicable_runbook")
            return [], status
        lexical = self._lexical_ranking(query, records) if strategy in {"bm25", "hybrid"} else []
        dense = []
        if strategy in {"vector", "hybrid"}:
            try:
                dense, cache = self.kb.breaker.call(lambda: self._dense_ranking(query, records))
                status.update(cache)
            except Exception as exc:
                status.update(strategy="bm25", degraded=True, reason=type(exc).__name__)
                if strategy == "vector":
                    lexical = self._lexical_ranking(query, records)
        if status["strategy"] == "hybrid":
            fused = {}
            for ranked in (lexical, dense):
                for rank, index in enumerate(ranked, 1):
                    fused[index] = fused.get(index, 0) + 1/(60+rank)
            selected = sorted(fused, key=lambda i: (-fused[i], records[i]["id"]))[:candidate_limit]
        else:
            selected = (dense if status["strategy"] == "vector" else lexical)[:candidate_limit]
        conflict_ids = self.conflicts(records)
        candidates = []
        query_codes = KnowledgeBase.local_codes(query)
        for rank, index in enumerate(selected, 1):
            row = dict(records[index])
            row["conflicted"] = row["id"] in conflict_ids
            row["snippet"] = row["text"]
            row["retrieval_rank"] = rank
            row["_exact_identifier"] = bool(query_codes.intersection(KnowledgeBase.local_codes(row.get("codes", ""))))
            if status["strategy"] == "hybrid":
                row["rrf_score"] = round(fused[index], 8)
                row["score"] = row["rrf_score"]
            candidates.append(row)
        # 向量召回成功时复用本轮 embedding；BM25-only 不会为了可选增强偷偷下载权重。
        if self.kb.semantic_dedup_enabled and dense:
            candidates, semantic_status = self._semantic_deduplicate(candidates)
        else:
            semantic_status = {"enabled": self.kb.semantic_dedup_enabled, "applied": False,
                               "removed": 0, "reason": "vector_unavailable" if self.kb.semantic_dedup_enabled else "disabled"}
        reranked, rerank_status = self.kb._rerank(query, candidates)
        result = reranked[:limit]
        if not rerank_status.get("applied"):
            # 保持旧的 RunbookKnowledge 返回契约；诊断页面通过 status 读取后处理细节。
            result = [{key: value for key, value in row.items()
                       if key not in {"snippet", "retrieval_rank", "rrf_score", "score", "_exact_identifier"}}
                      for row in result]
        else:
            result = [{key: value for key, value in row.items() if key != "_exact_identifier"}
                      for row in result]
        status["semantic_dedup"] = semantic_status
        status["rerank"] = rerank_status
        if semantic_status.get("degraded") or (rerank_status.get("degraded") and self.kb.rerank_mode == "required"):
            status["degraded"] = True
        if rerank_status.get("reason") == "below_threshold":
            status["reason"] = "rerank_below_threshold"
        status["elapsed_ms"] = round((time.perf_counter()-start)*1000, 3)
        return result, status
