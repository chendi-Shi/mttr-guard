import asyncio
import io
import json
import unittest
from unittest.mock import patch

from mttr_guard.agents import OllamaReasoner, investigate


class ModelAdapterTests(unittest.TestCase):
    def test_structured_request_and_response_contract(self):
        response = {"message": {"content": json.dumps({"verdict": "regression", "evidence_ids": ["e1"], "summary": "Canary-only errors"})}}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as http:
            finding = asyncio.run(OllamaReasoner("test-model").analyze("metrics", [{"id": "e1"}], []))
            req = http.call_args.args[0]
            payload = json.loads(req.data)
            self.assertEqual(req.full_url, "http://127.0.0.1:11434/api/chat")
            self.assertEqual(payload["model"], "test-model")
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["format"]["type"], "object")
            self.assertNotIn("tools", payload)
            self.assertEqual(finding.verdict, "regression")

    def test_oversized_response_is_rejected(self):
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"x" * 131073)):
            with self.assertRaises(ValueError):
                asyncio.run(OllamaReasoner("test-model").analyze("metrics", [{"id": "e1"}], []))

    def test_slow_agents_have_bounded_wait(self):
        class Slow:
            async def analyze(self, *args):
                await asyncio.sleep(1)
        findings = asyncio.run(investigate(Slow(), [], budget=.01))
        self.assertEqual(len(findings), 4)
        self.assertTrue(all(f.verdict == "unknown" for f in findings))


if __name__ == "__main__":
    unittest.main()
