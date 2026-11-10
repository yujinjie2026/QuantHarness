"""本地验证：所有模型请求均被拦截，不联网，不产生API费用。"""

import contextlib
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from openai.types.chat import ChatCompletion
from openai import BadRequestError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deepseek_adapter import DeepSeekChatOpenAI, InvalidDeepSeekResponse
from default_config import DEEPSEEK_RUNTIME_CONFIG
import run_btc as runner


def response(message, finish="stop"):
    return {
        "id": "offline-response", "object": "chat.completion", "created": 0,
        "model": "deepseek-flash", "choices": [
            {"index": 0, "message": message, "finish_reason": finish}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def client_options():
    return {
        "model": "deepseek-flash", "api_key": "offline-test-no-real-key",
        "base_url": "https://offline.invalid", "temperature": None,
        "reasoning_effort": "high", "disable_streaming": True,
        "use_responses_api": False, "streaming": False, "max_retries": 0,
        "extra_body": {"thinking": {"type": "enabled"}, "max_tokens": 16384},
    }


class ReproductionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"DEEPSEEK_API_KEY": "offline-test-no-real-key"})
        self.env.start()
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError(
            "本地验证禁止真实联网"
        ))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.env.stop()

    def test_official_agents_are_unchanged(self):
        expected = {
            "indicator_agent.py": "318a5b5e3aa2611852d3a7ddd088ed986102d96462b3e96b927247af6be1b0cd",
            "pattern_agent.py": "0c581d97249b4c60a004f37f18204dd618fe16b0637c5bb0c1ee677375261dd7",
            "trend_agent.py": "951edc675531ceec9579c5c82d26141c93fb5908ee18b90a34ba6b5ffa778bad",
            "decision_agent.py": "e55446b9edf2c273992987ca6b3fc5d55e366a7afbff23a564b0c5b6abeb3212",
        }
        for name, checksum in expected.items():
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), checksum)

    def test_all_data_and_score_direction(self):
        for sample_id in range(1, 101):
            history, future = runner.load_sample(sample_id)
            self.assertEqual((len(history), len(future)), (97, 3))
        history, future = runner.load_sample(1)
        reports = {field: "report" for field in runner.FIELDS}
        reports["final_trade_decision"] = '{"decision":"LONG"}'
        self.assertEqual(runner.score(reports, history, future)["hits"], 0)
        reports["final_trade_decision"] = '{"decision":"SHORT"}'
        self.assertEqual(runner.score(reports, history, future)["hits"], 3)
        reports["final_trade_decision"] = '{"decision":"HOLD"}'
        with self.assertRaises(ValueError):
            runner.score(reports, history, future)

    def test_reasoning_round_trip_and_image(self):
        llm = DeepSeekChatOpenAI(**client_options())
        raw = response({"role": "assistant", "content": "answer",
                        "reasoning_content": "offline-protocol-fixture"})
        for source in (raw, ChatCompletion.model_validate(raw)):
            result = llm._create_chat_result(source)
            self.assertEqual(result.generations[0].message.additional_kwargs[
                "reasoning_content"], "offline-protocol-fixture")
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
        messages = [
            HumanMessage(content=[{"type": "text", "text": "original text"}, image]),
            AIMessage(content="", additional_kwargs={"reasoning_content": ""}, tool_calls=[
                {"name": "compute_rsi", "args": {}, "id": "call1", "type": "tool_call"}
            ]),
            ToolMessage(content="42", tool_call_id="call1"),
            result.generations[0].message,
            HumanMessage(content="continue"),
        ]
        payload = llm._get_request_payload(messages)
        self.assertEqual(payload["messages"][0]["content"][1], image)
        self.assertEqual(payload["messages"][1]["reasoning_content"], "")
        self.assertEqual(payload["messages"][3]["reasoning_content"], "offline-protocol-fixture")
        self.assertNotIn("temperature", payload)
        self.assertNotIn("max_completion_tokens", payload)
        self.assertEqual(payload["extra_body"]["max_tokens"], 16384)
        self.assertFalse(payload["stream"])

    def test_truncated_response_is_not_accepted(self):
        llm = DeepSeekChatOpenAI(**client_options())
        with self.assertRaisesRegex(ValueError, "输出达到"):
            llm._create_chat_result(response({"role": "assistant", "content": "partial"}, "length"))

    def test_null_then_valid_retry_keeps_request_and_safe_diagnostics(self):
        captured = []

        def serve(request):
            captured.append(json.loads(request.content))
            if len(captured) == 1:
                return httpx.Response(200, content=b"null", headers={"content-type": "application/json", "x-request-id": "offline-null"})
            return httpx.Response(200, json=response({
                "role": "assistant", "content": "PRIVATE_RESPONSE_TEXT",
                "reasoning_content": "PRIVATE_REASONING_TEXT",
            }))

        with tempfile.TemporaryDirectory(prefix="quantagent-null-recovery-") as tmp:
            path = Path(tmp) / "api_diagnostics.jsonl"
            client = httpx.Client(transport=httpx.MockTransport(serve))
            llm = DeepSeekChatOpenAI(**client_options(), http_client=client,
                                    diagnostics_path=path, invalid_response_retry_delay=0)
            result = llm.invoke("PRIVATE_ORIGINAL_PROMPT")
            self.assertEqual(result.content, "PRIVATE_RESPONSE_TEXT")
            self.assertEqual(len(captured), 2)
            self.assertEqual(captured[0], captured[1])
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertTrue(any(row.get("json_type") == "null" for row in records))
            self.assertTrue(any(row.get("event") == "invalid_response_retry" for row in records))
            for private in ("PRIVATE_ORIGINAL_PROMPT", "PRIVATE_RESPONSE_TEXT", "PRIVATE_REASONING_TEXT", "offline-test-no-real-key"):
                self.assertNotIn(private, path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            client.close()

    def test_persistent_null_is_bounded_and_never_completes_sample(self):
        captured = []

        def serve(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})

        with tempfile.TemporaryDirectory(prefix="quantagent-null-failure-") as tmp:
            folder = Path(tmp) / "sample"
            client = httpx.Client(transport=httpx.MockTransport(serve))
            llm = DeepSeekChatOpenAI(**client_options(), http_client=client,
                                    invalid_response_retry_delay=0)

            class Graph:
                def stream(self, state, stream_mode):
                    llm.invoke("offline fixture")
                    yield {}

            class TradingGraph:
                graph = Graph()
                agent_llm = llm
                graph_llm = llm

            history, future = runner.load_sample(1)
            with patch.object(runner, "build_graph", return_value=TradingGraph()):
                with self.assertRaisesRegex(InvalidDeepSeekResponse, "NoneType"):
                    runner.run_sample(folder, 1, 1, history, future)
            self.assertEqual(len(captured), 2)
            self.assertFalse((folder / "completed.json").exists())
            error = json.loads((folder / "attempt_001/error.json").read_text())
            self.assertEqual(error["type"], "InvalidDeepSeekResponse")
            self.assertTrue(error["traceback"])
            self.assertTrue(client.is_closed)
            self.assertTrue(llm.root_async_client.is_closed())
            client.close()

    def test_malformed_and_empty_responses_are_rejected(self):
        llm = DeepSeekChatOpenAI(**client_options())
        for value in (None, "not json", {}, {"choices": None}, {"choices": []},
                      {"choices": [{"message": None}]},
                      response({"role": "assistant", "content": "", "reasoning_content": "fixture"}),
                      response({"role": "assistant", "content": "   "})):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(InvalidDeepSeekResponse):
                    llm._create_chat_result(value)

    def test_empty_content_with_valid_tool_call_is_accepted(self):
        llm = DeepSeekChatOpenAI(**client_options())
        raw = response({
            "role": "assistant", "content": None, "reasoning_content": "fixture",
            "tool_calls": [{"id": "tool1", "type": "function", "function": {
                "name": "compute_rsi", "arguments": "{}",
            }}],
        }, "tool_calls")
        result = llm._create_chat_result(raw)
        self.assertEqual(result.generations[0].message.tool_calls[0]["name"], "compute_rsi")

    def test_empty_http_body_is_bounded_and_reported_clearly(self):
        calls = []

        def serve(request):
            calls.append(1)
            return httpx.Response(200, content=b"", headers={"content-type": "application/json"})

        client = httpx.Client(transport=httpx.MockTransport(serve))
        llm = DeepSeekChatOpenAI(**client_options(), http_client=client,
                                invalid_response_retry_delay=0)
        with self.assertRaisesRegex(InvalidDeepSeekResponse, "空正文"):
            llm.invoke("original prompt")
        self.assertEqual(len(calls), 2)
        client.close()

    def test_http_error_is_not_misclassified_and_body_is_not_logged(self):
        calls = []

        def serve(request):
            calls.append(1)
            return httpx.Response(400, json={"error": {
                "message": "PRIVATE_SERVER_MESSAGE", "type": "invalid_request_error",
            }})

        with tempfile.TemporaryDirectory(prefix="quantagent-http-error-") as tmp:
            path = Path(tmp) / "api_diagnostics.jsonl"
            client = httpx.Client(transport=httpx.MockTransport(serve))
            llm = DeepSeekChatOpenAI(**client_options(), http_client=client,
                                    diagnostics_path=path, invalid_response_retry_delay=0)
            with self.assertRaises(BadRequestError):
                llm.invoke("PRIVATE_USER_PROMPT")
            self.assertEqual(len(calls), 1)
            text = path.read_text()
            self.assertNotIn("PRIVATE_SERVER_MESSAGE", text)
            self.assertNotIn("PRIVATE_USER_PROMPT", text)
            self.assertTrue(any(json.loads(line).get("http_status") == 400
                                for line in text.splitlines()))
            client.close()

    def test_length_error_does_not_retry_or_change_model_parameters(self):
        calls = []

        def serve(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json=response({"role": "assistant", "content": "partial"}, "length"))

        client = httpx.Client(transport=httpx.MockTransport(serve))
        llm = DeepSeekChatOpenAI(**client_options(), http_client=client,
                                invalid_response_retry_delay=0)
        with self.assertRaisesRegex(ValueError, "输出达到"):
            llm.invoke("original prompt")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["thinking"], {"type": "enabled"})
        self.assertEqual(calls[0]["max_tokens"], 16384)
        client.close()

    def test_async_null_recovery_uses_same_protocol(self):
        async def check():
            calls = []

            def serve(request):
                calls.append(json.loads(request.content))
                if len(calls) == 1:
                    return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
                return httpx.Response(200, json=response({"role": "assistant", "content": "ok", "reasoning_content": "fixture"}))

            with tempfile.TemporaryDirectory(prefix="quantagent-async-recovery-") as tmp:
                async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
                    llm = DeepSeekChatOpenAI(**client_options(), http_async_client=client,
                                            diagnostics_path=Path(tmp) / "api_diagnostics.jsonl",
                                            invalid_response_retry_delay=0)
                    result = await llm.ainvoke("original prompt")
                    self.assertEqual(result.content, "ok")
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(calls[0], calls[1])
        asyncio.run(check())

    def test_full_graph_with_mock_http_preserves_original_prompt(self):
        import trading_graph
        from graph_util import TechnicalTools

        captured = []
        models = []

        def serve(request):
            body = json.loads(request.content)
            captured.append(body)
            self.assertNotIn("temperature", body)
            self.assertNotIn("max_completion_tokens", body)
            self.assertEqual(body["thinking"], {"type": "enabled"})
            self.assertEqual(body["max_tokens"], 16384)
            names = [tool["function"]["name"] for tool in body.get("tools", [])]
            if names:
                for message in body["messages"]:
                    if message["role"] == "assistant":
                        self.assertIn("reasoning_content", message)
                if names[0].startswith("compute_") and body["messages"][-1]["role"] == "tool":
                    content, tool_calls = "Indicator report", None
                else:
                    content = ""
                    tool_calls = [{
                        "id": f"call_{len(captured)}", "type": "function",
                        "function": {"name": names[0], "arguments": "{}"},
                    }]
            else:
                text = str(body["messages"])
                content = ('{"decision":"LONG","risk_reward_ratio":1.6}'
                           if "immediate execution order" in text else "Visual analysis report")
                tool_calls = None
            message = {"role": "assistant", "content": content,
                       "reasoning_content": "offline-protocol-fixture"}
            if tool_calls:
                message["tool_calls"] = tool_calls
            return httpx.Response(200, json=response(message, "tool_calls" if tool_calls else "stop"))

        def make_client(**kwargs):
            kwargs["api_key"] = "offline-test-no-real-key"
            kwargs["base_url"] = "https://offline.invalid"
            kwargs["http_client"] = httpx.Client(transport=httpx.MockTransport(serve))
            model = DeepSeekChatOpenAI(**kwargs)
            models.append(model)
            return model

        history, future = runner.load_sample(1)
        with tempfile.TemporaryDirectory(prefix="quantagent-graph-test-") as tmp:
            folder = Path(tmp) / "sample"
            with patch.object(trading_graph, "DeepSeekChatOpenAI", side_effect=make_client), \
                 patch.object(TechnicalTools.generate_kline_image, "func", return_value={"pattern_image": "AA=="}), \
                 patch.object(TechnicalTools.generate_trend_image, "func", return_value={"trend_image": "AA=="}), \
                 contextlib.redirect_stdout(io.StringIO()):
                runner.run_sample(folder, 1, 1, history, future)
            self.assertTrue(all(model.http_client.is_closed for model in models))
            self.assertTrue(all(model.http_async_client.is_closed for model in models))
            saved = (folder / "attempt_001/decision_prompt.txt").read_text()
            self.assertEqual(captured[-1]["messages"], [{"role": "user", "content": saved}])
            self.assertNotIn("Benchmark clarification", saved)
            self.assertIn("N = 1", saved)
            self.assertIsNotNone(runner.read_completed(folder, history, future))
            (folder / "attempt_001/reports.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "被修改"):
                runner.read_completed(folder, history, future)

    def test_factory_and_fingerprint_record_effective_parameters(self):
        import trading_graph
        graph = trading_graph.TradingGraph()
        self.assertEqual(set(graph.graph.get_graph().nodes) - {"__start__", "__end__"},
                         {"Indicator Agent", "Pattern Agent", "Trend Agent", "Decision Maker"})
        payload = graph.graph_llm._get_request_payload("unchanged prompt")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "unchanged prompt"}])
        self.assertIsNone(graph.graph_llm.temperature)
        saved = runner.fingerprint()
        self.assertEqual(saved["deepseek_runtime"], DEEPSEEK_RUNTIME_CONFIG)
        self.assertIsNone(saved["temperature_sent"])
        self.assertIn("deepseek_adapter.py", saved["source_sha256"])

    def test_interruption_resume_and_fingerprint_guard(self):
        calls = []

        class FakeGraph:
            def stream(self, state, stream_mode):
                self_count = len(state["kline_data"]["Close"])
                if self_count != 97:
                    raise AssertionError("future data reached agent")
                calls.append(1)
                if len(calls) == 2:
                    raise KeyboardInterrupt()
                yield {"all": dict(zip(runner.FIELDS, [
                    "indicator", "pattern", "trend", '{"decision":"LONG"}'
                ]))}

        class FakeTradingGraph:
            graph = FakeGraph()

        with tempfile.TemporaryDirectory(prefix="quantagent-resume-test-") as tmp:
            root = Path(tmp)
            for path in ROOT.glob("*.py"):
                shutil.copy2(path, root / path.name)
            shutil.copytree(ROOT / "benchmark/btc", root / "benchmark/btc")
            with patch.object(runner, "ROOT", root), \
                 patch.object(runner, "build_graph", return_value=FakeTradingGraph()), \
                 patch.object(sys, "argv", ["run_btc.py", "--resume"]), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    runner.main()
                output = root / "results/formal/baseline"
                partial = json.loads((output / "summary.json").read_text())
                self.assertFalse(partial["complete"])
                self.assertEqual(partial["completed_samples"], 1)
                runner.main()
                final = json.loads((output / "summary.json").read_text())
                self.assertEqual(final["completed_samples"], 300)
                self.assertTrue(final["complete"])
                self.assertEqual(len(calls), 301)
                runner.main()
                self.assertEqual(len(calls), 301)
                changed = root / "deepseek_adapter.py"
                changed.write_text(changed.read_text() + "\n# different config\n")
                with self.assertRaisesRegex(ValueError, "不能混入"):
                    runner.main()
                self.assertEqual(len(calls), 301)


if __name__ == "__main__":
    unittest.main()
