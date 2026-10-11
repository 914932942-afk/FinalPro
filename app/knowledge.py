"""本地 Markdown 分块、开源 BGE embedding、Chroma 持久化语义检索。"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections import Counter
from pathlib import Path

from .reliability import CircuitBreaker
from .config import get_settings
from .errors import describe_error

class KnowledgeBase:
    def __init__(self, directory: Path, *, backend=None, persist_directory=None,
                 embedding_model=None, embedder=None, chunk_size=None, overlap=None, hybrid=None, settings=None,
                 purpose="planning", reranker=None):
        cfg = settings or get_settings()
        chunk_size = cfg.rag_chunk_size if chunk_size is None else chunk_size
        overlap = cfg.rag_overlap if overlap is None else overlap
        if chunk_size <= overlap or overlap < 0:
            raise ValueError("chunk_size must be greater than overlap >= 0")
        self.directory = Path(directory)
        self.purpose = purpose

        self.directory.mkdir(parents=True, exist_ok=True)
        self.backend = backend or cfg.rag_backend
        if self.backend not in {"chroma", "lexical"}:
            raise ValueError("AGENT_RAG_BACKEND must be chroma or lexical")
        local_default = self.directory.parent / "models" / "bge-small-zh-v1.5"
        configured = embedding_model or cfg.embedding_model
        self.model_name = configured or (str(local_default) if (local_default / "config.json").exists() else "BAAI/bge-small-zh-v1.5")
        self.persist = Path(persist_directory or (cfg.chroma_dir if self.directory.resolve() == cfg.kb_dir
                                                  else self.directory.parent / "chroma"))
        self.embedder = embedder
        self.hybrid = cfg.rag_hybrid if hybrid is None else hybrid
        self.semantic_dedup_enabled = cfg.rag_semantic_dedup
        self.semantic_dedup_threshold = cfg.rag_semantic_dedup_threshold
        self.rerank_mode = cfg.rag_rerank_mode
        configured_reranker = cfg.rag_reranker_model
        bundled_reranker = cfg.data_dir / "models" / "bge-reranker-base"
        self.reranker_model_name = (str(bundled_reranker) if configured_reranker == "BAAI/bge-reranker-base"
                                    and (bundled_reranker / "config.json").is_file() else configured_reranker)
        self.rerank_candidate_k = cfg.rag_rerank_candidate_k
        self.rerank_min_score = cfg.rag_rerank_min_score
        self.reranker = reranker
        self._reranker_attempted = reranker is not None
        self.chunk_size, self.overlap = chunk_size, overlap
        self._lock = threading.RLock()
        self.breaker = CircuitBreaker(cfg.rag_failure_threshold, cfg.rag_recovery_timeout)
        self.top_k, self.batch_size = cfg.rag_top_k, cfg.rag_batch_size
        self.collection = None
        self.documents = self._load_documents()

    def _load_documents(self):
        docs = []
        for path in sorted(self.directory.rglob("*.md")):
            source = path.relative_to(self.directory).as_posix()
            text = path.read_text(encoding="utf-8")
            metadata = self._metadata(text)
            if metadata.get("kind", "planning") != self.purpose:
                continue
            if metadata.get("status", "active") != "active":
                continue
            header = re.match(r"<!-- rag: (.*?) -->\s*", text, re.S)
            beginning = header.end() if header else 0

            if self.purpose == "runbook":

                body = text[beginning:].strip()
                if len(body) > 12000:
                    raise ValueError("split long runbooks into complete applicability units")
                docs.append({"id": metadata["document_id"], "title": path.stem, "source": source,
                             "offset": beginning, "text": body, **metadata,
                             "content_hash": hashlib.sha256(text.encode()).hexdigest()})
                continue
            boundaries = sorted({beginning, len(text), *[m.start() for m in re.finditer(r"(?m)^#{1,6} ", text) if m.start() >= beginning]})
            for left, right in zip(boundaries, boundaries[1:]):
                for start in range(left, right, self.chunk_size - self.overlap):
                    chunk = text[start:min(start + self.chunk_size, right)]
                    if not chunk.strip():
                        continue
                    identity = f"{source}:{start}:{chunk}:{json.dumps(metadata, sort_keys=True)}"
                    docs.append({"id": hashlib.sha256(identity.encode()).hexdigest(),
                                 "title": path.stem.replace("_", " "), "source": source,
                                 "offset": start, "text": chunk, **metadata})
        if len({d["id"] for d in docs}) != len(docs):
            raise ValueError("duplicate document identity")
        return docs

    @staticmethod
    def _metadata(text):
        match = re.match(r"<!-- rag: (.*?) -->", text, re.S)
        if not match:
            return {}
        metadata = json.loads(match[1])
        if (not isinstance(metadata, dict) or set(metadata) - {"resource", "codes", "status", "version", "kind",
                "document_id", "environment", "family", "supersedes", "provenance", "service",
                "owner_team", "severity", "reviewed_at"}
                or any(not isinstance(v, str) for v in metadata.values())
                or metadata.get("status", "active") not in {"active", "retired"}):
            raise ValueError("invalid SOP metadata")
        if metadata.get("kind") == "runbook" and not all(metadata.get(k) for k in
                ("document_id", "resource", "version", "environment", "family", "provenance")):
            raise ValueError("runbook requires scope, version and provenance")
        return metadata

    def _encode(self, texts):
        if self.embedder is None:
            from sentence_transformers import SentenceTransformer

            self.embedder = SentenceTransformer(self.model_name, device="cpu",
                                               local_files_only=self.purpose == "runbook")
        return self.embedder.encode(texts, normalize_embeddings=True).tolist()

    @staticmethod
    def _normalized_text(text):
        return re.sub(r"\s+", " ", str(text or "").strip().casefold())

    @classmethod
    def _scope_key(cls, item):

        return tuple(item.get(key, "") for key in ("resource", "environment", "family", "version", "codes"))

    @classmethod
    def _has_applicability_metadata(cls, item):
        return any(item.get(key) for key in ("resource", "environment", "family", "version", "codes"))

    @classmethod
    def _exact_deduplicate(cls, items):
        """按身份和同一适用范围内的规范化正文去重，保留首次出现的候选。"""
        selected, seen_ids, seen_content = [], set(), set()
        removed = 0
        for item in items:
            ident = item.get("id")
            content = cls._normalized_text(item.get("snippet", item.get("text", "")))
            content_key = (content, cls._scope_key(item)) if content else None
            if ident in seen_ids or (content_key and content_key in seen_content):
                removed += 1
                continue
            if ident:
                seen_ids.add(ident)
            if content_key:
                seen_content.add(content_key)
            selected.append(item)
        return selected, removed

    def _semantic_deduplicate(self, query, items, *, vectors_available=False):
        """对已过滤候选做保守语义去重；不同适用范围即使相似也必须保留。"""
        if not self.semantic_dedup_enabled or len(items) < 2:
            return items, {"enabled": self.semantic_dedup_enabled, "applied": False,
                           "removed": 0, "reason": "not_needed"}

        if not vectors_available and self.embedder is None:
            return items, {"enabled": True, "applied": False, "removed": 0,
                           "reason": "embedding_unavailable"}
        try:
            vectors = self._encode([item.get("snippet", item.get("text", "")) for item in items])
            if len(vectors) != len(items) or any(not vector or any(not math.isfinite(x) for x in vector)
                                                  for vector in vectors):
                raise ValueError("invalid semantic dedup embeddings")
            keep, keep_vectors = [], []
            removed = 0
            for item, vector in zip(items, vectors):
                duplicate = False
                for prior, prior_vector in zip(keep, keep_vectors):
                    if not self._has_applicability_metadata(item) or not self._has_applicability_metadata(prior):
                        continue
                    if self._scope_key(item) != self._scope_key(prior):
                        continue
                    score = sum(a * b for a, b in zip(vector, prior_vector))
                    if score >= self.semantic_dedup_threshold:
                        duplicate = True
                        removed += 1
                        break
                if not duplicate:
                    keep.append(item)
                    keep_vectors.append(vector)
            return keep, {"enabled": True, "applied": True, "removed": removed,
                          "threshold": self.semantic_dedup_threshold}
        except Exception as exc:
            return items, {"enabled": True, "applied": False, "removed": 0,
                           "degraded": True, "reason": type(exc).__name__}

    def _load_reranker(self):
        if self.reranker is not None:
            return self.reranker
        if self._reranker_attempted:
            raise RuntimeError("reranker unavailable")
        self._reranker_attempted = True
        from sentence_transformers import CrossEncoder

        self.reranker = CrossEncoder(self.reranker_model_name, device="cpu", local_files_only=True)
        return self.reranker

    def _rerank(self, query, items):
        status = {"mode": self.rerank_mode, "applied": False, "model": self.reranker_model_name}
        if self.rerank_mode == "off" or not items:
            status["reason"] = "disabled" if self.rerank_mode == "off" else "no_candidates"
            return items, status
        try:
            model = self._load_reranker()
            pairs = [(query, item.get("snippet", item.get("text", ""))) for item in items]
            scores = model.predict(pairs)
            scores = [float(score) for score in scores]
            if len(scores) != len(items) or any(not math.isfinite(score) for score in scores):
                raise ValueError("invalid reranker scores")
            ranked = []
            for item, score in zip(items, scores):
                row = dict(item)
                row["rerank_score"] = round(score, 6)
                ranked.append(row)
            accepted = [row for row in ranked if row["rerank_score"] >= self.rerank_min_score]
            accepted.sort(key=lambda row: (-int(bool(row.get("_exact_identifier"))),
                                          -row["rerank_score"], -float(row.get("score", 0)), row.get("id", "")))
            status.update(applied=True, accepted=len(accepted), threshold=self.rerank_min_score)
            if not accepted:
                status["reason"] = "below_threshold"
            return accepted, status
        except Exception as exc:
            status.update(degraded=True, fallback="rrf_or_retrieval_order", reason=type(exc).__name__)
            if self.rerank_mode == "required":
                return [], status
            return items, status

    def _post_process(self, query, items, limit, *, vectors_available=False):
        items, exact_removed = self._exact_deduplicate(items)
        items, semantic = self._semantic_deduplicate(query, items, vectors_available=vectors_available)

        rerank_input = items[:max(limit, self.rerank_candidate_k)]
        items, rerank = self._rerank(query, rerank_input)
        status = {"exact_duplicates_removed": exact_removed, "semantic_dedup": semantic,
                  "rerank": rerank}
        if semantic.get("degraded") or (rerank.get("degraded") and self.rerank_mode == "required"):
            status["degraded"] = True
        return [{key: value for key, value in item.items() if key != "_exact_identifier"}
                for item in items[:limit]], status

    def _index(self):
        import chromadb
        from chromadb.config import Settings
        self.client = chromadb.PersistentClient(path=str(self.persist),
                                                settings=Settings(anonymized_telemetry=False))
        name = "sop-" + hashlib.sha256((str(self.directory.resolve()) + self.model_name + self.purpose).encode()).hexdigest()[:20]
        self.collection = self.client.get_or_create_collection(name, embedding_function=None,
                                                               metadata={"hnsw:space": "cosine"})
        stored = self.collection.get(include=["metadatas"])
        existing = set(stored["ids"])
        hashes = {key: (meta or {}).get("content_hash") for key, meta in zip(stored["ids"], stored["metadatas"])}
        wanted = {d["id"] for d in self.documents}
        new = [d for d in self.documents if d["id"] not in existing or
               d.get("content_hash") and hashes.get(d["id"]) != d["content_hash"]]
        for start in range(0, len(new), self.batch_size):
            batch = new[start:start + self.batch_size]
            self.collection.upsert(ids=[d["id"] for d in batch],
                embeddings=self._encode([d["text"] for d in batch]),
                documents=[d["text"] for d in batch],
                metadatas=[{k: d[k] for k in ("title", "source", "offset", "resource", "codes", "status", "version",
                    "kind", "document_id", "environment", "family", "provenance", "supersedes", "service",
                    "owner_team", "severity", "reviewed_at", "content_hash") if k in d} for d in batch])
        if existing - wanted:
            self.collection.delete(ids=list(existing - wanted))
        return {"chunks": len(self.documents), "backend": "chroma"}

    def reindex(self):
        with self._lock:
            self.documents = self._load_documents()
            try:
                return self.breaker.call(self._index)
            except Exception:
                self.collection = None
                raise

    @staticmethod
    def _tokens(text):
        return set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.lower()))

    def lexical_search(self, query, limit=3):
        q = self._tokens(query)
        ranked = []
        for doc in self.documents:
            overlap = len(q & self._tokens(doc["text"]))
            if overlap:
                ranked.append({**{k: v for k, v in doc.items() if k != "text"},
                               "score": overlap / max(len(q), 1), "snippet": doc["text"],
                               "retrieval_backend": "lexical"})
        return sorted(ranked, key=lambda item: item["score"], reverse=True)[:limit]

    @staticmethod
    def _terms(text):
        words = re.findall(r"[a-z0-9_]+(?:-[a-z0-9_]+)*|[\u4e00-\u9fff]+", text.lower())
        return [term for word in words for term in
                ([word] if word.isascii() or len(word) == 1 else [word[i:i + 2] for i in range(len(word) - 1)])]

    def bm25_search(self, query, limit=12):
        counts = [Counter(self._terms(doc["text"])) for doc in self.documents]
        if not counts:
            return []
        lengths = [sum(counter.values()) for counter in counts]
        average = sum(lengths) / len(lengths) or 1
        terms = set(self._terms(query))
        frequencies = {term: sum(term in count for count in counts) for term in terms}
        ranked = []
        for doc, count, length in zip(self.documents, counts, lengths):
            score = 0.0
            for term in terms:
                tf = count[term]
                idf = math.log(1 + (len(counts) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / average))
            if score > 0:
                ranked.append({**{k: v for k, v in doc.items() if k != "text"}, "snippet": doc["text"],
                               "score": score, "retrieval_backend": "bm25"})
        return sorted(ranked, key=lambda item: (-item["score"], item["id"]))[:limit]

    @staticmethod
    def local_codes(text):
        return set(re.findall(r"(?i)\b[A-Z]{1,8}[-_]?\d{3,6}\b", text.upper()))

    def _fuse(self, query, vector, lexical, limit):
        from .policy import resource_catalog, contains
        resources = {key for key, value in resource_catalog().items() if any(contains(query, alias) for alias in value["aliases"])}
        codes = self.local_codes(query)
        results = {}
        content_ids = {}
        for name, ranked in (("vector", vector), ("bm25", lexical)):
            for rank, item in enumerate(ranked, 1):

                if item.get("resource") and resources and item["resource"] not in resources:
                    continue
                item_codes = self.local_codes(item.get("codes", ""))
                if item_codes and codes and not codes.intersection(item_codes):
                    continue
                content_key = (self._normalized_text(item.get("snippet", item.get("text", ""))),
                               self._scope_key(item))
                existing_id = content_ids.get(content_key) if content_key[0] else None
                target_id = existing_id or item["id"]
                content_ids.setdefault(content_key, target_id)
                entry = results.setdefault(target_id, {**item, "_exact_identifier": bool(codes.intersection(item_codes)), "score": 0.0,
                    "retrieval_backend": "hybrid" if vector else "bm25", "ranks": {}})
                entry["score"] += 1 / (60 + rank)
                entry["ranks"][name] = rank

        return sorted(results.values(), key=lambda item: (
            -bool(codes.intersection(self.local_codes(item.get("codes", "")))), -item["score"], item["id"]))[:limit]

    def _vector_search(self, query, limit):
        with self._lock:

            if self.collection is None:
                try:
                    self._index()
                except Exception:
                    self.collection = None
                    raise
            if not self.documents:
                return []
            result = self.collection.query(query_embeddings=self._encode([query]),
                n_results=min(limit, len(self.documents)), include=["documents", "metadatas", "distances"])
            return [{"id": ident, **meta, "score": round(1 - distance, 4), "snippet": text,
                     "retrieval_backend": "chroma"}
                    for ident, text, meta, distance in zip(result["ids"][0], result["documents"][0],
                                                          result["metadatas"][0], result["distances"][0])]

    def search_with_status(self, query, limit=None):
        limit = self.top_k if limit is None else limit
        if limit < 1:
            return [], {"backend": self.backend}
        candidate_limit = max(limit, self.rerank_candidate_k)
        if self.backend == "lexical":
            if self.hybrid:
                candidates = self._fuse(query, [], self.bm25_search(query, max(candidate_limit * 4, 12)), candidate_limit)
                docs, post = self._post_process(query, candidates, limit)
                return docs, {"backend": "lexical", "strategy": "bm25", **post}
            candidates = self.lexical_search(query, candidate_limit)
            docs, post = self._post_process(query, candidates, limit)
            return docs, {"backend": "lexical", **post}
        try:
            if not self.hybrid:
                candidates = self.breaker.call(lambda: self._vector_search(query, candidate_limit))
                docs, post = self._post_process(query, candidates, limit, vectors_available=True)
                return docs, {"backend": "chroma", **post}
            window = max(candidate_limit * 4, 12)
            vector = self.breaker.call(lambda: self._vector_search(query, window))
            candidates = self._fuse(query, vector, self.bm25_search(query, window), candidate_limit)
            docs, post = self._post_process(query, candidates, limit, vectors_available=True)
            return docs, {"backend": "chroma", "strategy": "hybrid", **post}
        except Exception as exc:
            fallback_candidates = (self._fuse(query, [], self.bm25_search(query, max(candidate_limit * 4, 12)), candidate_limit)
                                   if self.hybrid else self.lexical_search(query, candidate_limit))
            fallback, post = self._post_process(query, fallback_candidates, limit)
            return fallback, {
                "backend": "bm25" if self.hybrid else "lexical", "degraded": True, "reason": type(exc).__name__,
                "error_info": describe_error(exc, "rag").to_dict(), **post}

    def search(self, query, limit=None):
        return self.search_with_status(query, limit)[0]

    def planning_context(self, query):
        """Exact policies are catalog lookups; generic routes use fixed policy documents.

        Return ALL matching policy fragments so top-k cannot hide a conflict.
        Semantic candidates are not authority to infer an unobserved error code.
        """
        from .policy import resource_catalog, contains
        codes = self.local_codes(query)
        resources = {key for key, value in resource_catalog().items()
                     if any(contains(query, alias) for alias in value["aliases"])}
        docs = [d for d in self.documents if not d.get("codes") or
                (codes.intersection(self.local_codes(d["codes"])) and
                 (not resources or d.get("resource") in resources))]
        return [{**{k: v for k, v in d.items() if k != "text"}, "snippet": d["text"],
                 "retrieval_backend": "policy_catalog"} for d in docs], {"backend": "policy_catalog"}
