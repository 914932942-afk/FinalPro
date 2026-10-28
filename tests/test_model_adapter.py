"""模型适配层测试：不访问真实网络，也能验证 DeepSeek 请求契约。

测试用假的 urlopen 返回值替代互联网。这样每次运行都稳定、免费，
同时能检查请求地址、认证头、JSON 模式和响应字段校验。
"""

import json
import unittest
from unittest.mock import patch

from app.model_adapter import (
    DeepSeekDecisionModel,
    ModelOutputError,
    ResilientDecisionModel,
    RuleBasedModel,
)


class FakeResponse:
    """模拟 urllib 响应对象，只实现适配器实际用到的两个方法。"""

    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class ModelAdapterTests(unittest.TestCase):
    """覆盖成功解析、非法字段和回退三类关键行为。"""

    def test_deepseek_request_and_json_output(self):
        """成功响应应被解析为 Decision，并发送 OpenAI 兼容 JSON 请求。"""
        response = {
            "model": "deepseek-flash",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "category": "incident",
                                "action": "open_incident",
                                "confidence": 0.91,
                                "rationale": "服务不可用",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }
        with patch("app.model_adapter.urlopen", return_value=FakeResponse(response)) as mocked:
            decision = DeepSeekDecisionModel("test-secret", max_retries=0).decide(
                "数据库宕机", "生产服务返回 500", "high", [{"title": "SOP", "snippet": "升级故障"}]
            )

        request = mocked.call_args.args[0]
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        # 接口默认开启思考；客户端应明确选择适合短 JSON 决策的非思考模式。
        self.assertEqual(sent["thinking"], {"type": "disabled"})
        self.assertEqual(sent["model"], "deepseek-flash")
        self.assertEqual(decision.source, "deepseek")
        self.assertEqual(decision.action, "open_incident")
        self.assertEqual(decision.metadata["usage"]["total_tokens"], 30)

    def test_model_payload_redacts_ticket_secrets_before_network(self):
        response = {"choices": [{"message": {"content": json.dumps({
            "category": "incident", "action": "open_incident", "confidence": 0.9,
            "rationale": "有据登记",
        }, ensure_ascii=False)}}]}
        with patch("app.model_adapter.urlopen", return_value=FakeResponse(response)) as mocked:
            DeepSeekDecisionModel("test-secret", max_retries=0).decide(
                "VPN 故障", "Authorization: Bearer abcdefghijklmnop1234 password=s3cret", "high"
            )
        sent = json.loads(mocked.call_args.args[0].data.decode("utf-8"))
        content = sent["messages"][1]["content"]
        self.assertNotIn("abcdefghijklmnop1234", content)
        self.assertNotIn("s3cret", content)
        self.assertIn("VPN 故障", content)

    def test_invalid_model_output_is_rejected(self):
        """不在白名单中的动作不能进入工具执行层。"""
        with self.assertRaises(ModelOutputError):
            DeepSeekDecisionModel._validate_decision(
                {
                    "category": "incident",
                    "action": "delete_everything",
                    "confidence": 1,
                    "rationale": "恶意输出",
                }
            )

    def test_resilient_model_falls_back_without_network(self):
        """真实模型抛错时，容错包装器返回明确的 rule_fallback 结果。"""
        class BrokenModel:
            def decide(self, *_args, **_kwargs):
                raise RuntimeError("network down")

        decision = ResilientDecisionModel(BrokenModel(), RuleBasedModel()).decide(
            "密码重置", "无法登录"
        )
        self.assertEqual(decision.source, "rule_fallback")
        self.assertEqual(decision.action, "reset_password")

    def test_resilient_model_strict_mode_propagates_error(self):
        """严格模式不吞掉异常，交给 workflow 决定如何标记工单。"""

        class BrokenModel:
            def decide(self, *_args, **_kwargs):
                raise RuntimeError("network down")

        model = ResilientDecisionModel(BrokenModel(), strict=True)
        with self.assertRaises(RuntimeError):
            model.decide("测试", "测试")


if __name__ == "__main__":
    unittest.main()
