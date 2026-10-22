import copy
import json
from pathlib import Path
import tempfile
import unittest
from app.config import get_settings
from app.human_review import ReviewPipeline,write_new,digest

ROOT=Path(__file__).resolve().parents[1]


class HumanReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.cfg=get_settings(_env_file=None,runbooks_dir=ROOT/"data/knowledge/runbooks")
        self.pipeline=ReviewPipeline(self.cfg)
        self.fixture=ROOT/"evals/retrieval_validation_v3.json"

    def tearDown(self):self.temp.cleanup()

    def test_freeze_retains_self_authorship_and_rejects_bad_labels(self):
        frozen=self.pipeline.freeze(self.fixture,"developer")
        self.assertFalse(frozen["independent_author_declared"])
        self.assertFalse(frozen["independence_verified"])
        data=copy.deepcopy(frozen["fixture"]);data["cases"][0]["expected_runbook"]="invented"
        path=self.root/"bad.json";write_new(path,data)
        with self.assertRaises(ValueError):self.pipeline.freeze(path,"tester")

    def test_blinding_missing_reviews_and_tampered_answers(self):
        frozen=self.pipeline.freeze(self.fixture,"developer");fp=self.root/"frozen.json";write_new(fp,frozen)
        ep=self.root/"evaluation.json"
        write_new(ep,{"fixture_sha256":digest(self.fixture),"corpus_sha256":frozen["corpus_sha256"],"records":[
            {"id":"v3-01","variant":"vector","retrieved_ids":["kb-v01"],"answer":{"summary":"test"},"judgment":"hidden"}]})
        packet,mapping=self.pipeline.packet(fp,ep)
        self.assertNotIn("judgment",packet["items"][0])
        self.assertNotIn("variant",packet["items"][0])
        pp=self.root/"packet.json";mp=self.root/"mapping.json";write_new(pp,packet);write_new(mp,mapping)
        score=self.pipeline.score(pp,pp,mp)
        self.assertFalse(score["complete"]);self.assertEqual(score["summary"]["vector"]["passed"],0)
        changed=copy.deepcopy(packet);changed["items"][0]["answer"]["summary"]="different"
        rp=self.root/"review.json";write_new(rp,changed)
        with self.assertRaises(ValueError):self.pipeline.score(pp,rp,mp)

    def test_frozen_and_evaluation_hashes_must_match(self):
        fp=self.root/"frozen.json";write_new(fp,self.pipeline.freeze(self.fixture,"developer"))
        ep=self.root/"evaluation.json";write_new(ep,{"fixture_sha256":"wrong","records":[]})
        with self.assertRaises(ValueError):self.pipeline.packet(fp,ep)
