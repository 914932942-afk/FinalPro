"""真实 Chroma 存取契约；注入确定性 embedding，测试无需下载权重。"""
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.knowledge import KnowledgeBase


class TinyEmbedding:
    def encode(self, texts, **kwargs):
        return np.array([[1., 0., 0.] if '密码' in text else [0., 1., 0.] for text in texts])


class RagTests(unittest.TestCase):
    def test_index_query_restart_update_delete_and_citations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kb = root / 'kb'
            kb.mkdir()
            (kb / 'password.md').write_text('密码重置流程，需要验证身份。', encoding='utf-8')
            (kb / 'access.md').write_text('生产权限申请需要经理批准。', encoding='utf-8')
            store = KnowledgeBase(kb, backend='chroma', embedder=TinyEmbedding(), embedding_model='test-vector-v1')
            try:
                result = store.reindex()
                self.assertEqual(result['chunks'], 2)
                docs, status = store.search_with_status('密码忘了', 1)
                self.assertEqual(status['backend'], 'chroma')
                self.assertEqual(docs[0]['source'], 'password.md')
                self.assertIn('验证身份', docs[0]['snippet'])
                restored = KnowledgeBase(kb, backend='chroma', embedder=TinyEmbedding(), embedding_model='test-vector-v1')
                self.assertEqual(restored.search('密码忘了', 1)[0]['id'], docs[0]['id'])
                (kb / 'password.md').write_text('密码更新流程。', encoding='utf-8')
                (kb / 'access.md').unlink()
                store.reindex()
                self.assertEqual(store.collection.count(), 1)
                self.assertIn('更新流程', store.search('密码忘了')[0]['snippet'])
                (kb / 'password.md').unlink()
                store.reindex()
                self.assertEqual(store.collection.count(), 0)
                self.assertEqual(store.search('密码忘了'), [])
            finally:
                if getattr(store, 'client', None):
                    store.client._system.stop()

    def test_chunking_retains_source_and_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'doc.md').write_text('abcdefghijklmno', encoding='utf-8')
            kb = KnowledgeBase(path, backend='lexical', chunk_size=10, overlap=3)
            self.assertEqual(kb.documents[0]['text'][-3:], kb.documents[1]['text'][:3])
            self.assertEqual(kb.documents[1]['offset'], 7)
