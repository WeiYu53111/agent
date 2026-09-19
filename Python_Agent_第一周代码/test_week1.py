"""D06：八个核心用例 + 执行边界与接口适配检查。全部离线。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from calculator import TOOL, calculator, execute
from model import ANSWER_FORMAT, MockModel, OpenAIModel, Reply, call, final
from runtime import Limits, run


class CoreEight(unittest.IsolatedAsyncioTestCase):
    async def test_01_direct_answer(self):
        r = await run(MockModel([final("你好")]), "问好", log_dir=None)
        self.assertEqual((r["status"], r["tool_calls"], r["answer"]), ("completed", 0, "你好"))

    async def test_02_calculation_and_call_id(self):
        model = MockModel()
        with tempfile.TemporaryDirectory() as folder:
            r = await run(model, "17 × 23", log_dir=folder)
            events = [json.loads(line) for line in Path(r["log_path"]).read_text().splitlines()]
        self.assertEqual((r["answer"], r["model_calls"], r["tool_calls"]), ("391", 2, 1))
        request = next(e for e in events if e["event"] == "tool_request")
        result = next(e for e in events if e["event"] == "tool_result")
        self.assertEqual(request["call_id"], result["call_id"])
        self.assertEqual(result["result"]["value"], 391)
        feedback = model.inputs[1][-1]
        self.assertEqual(feedback["type"], "function_call_output")
        self.assertEqual(feedback["call_id"], request["call_id"])

    async def test_03_bad_operator(self):
        r = await run(MockModel([call({"operation": "power", "a": 2, "b": 3})]), "计算", log_dir=None)
        self.assertEqual((r["status"], r["stop_reason"]), ("failed", "tool_error"))
        self.assertEqual(calculator({"operation": "power", "a": 2, "b": 3})["error"]["code"], "INVALID_OPERATION")

    async def test_04_bad_type(self):
        r = await run(MockModel([call({"operation": "add", "a": "2", "b": 3})]), "计算", log_dir=None)
        self.assertEqual(r["stop_reason"], "tool_error")
        self.assertEqual(calculator({"operation": "add", "a": True, "b": 3})["error"]["code"], "INVALID_TYPE")

    async def test_05_tool_crash(self):
        def broken(args):
            raise RuntimeError("secret-like internal detail must not leak")
        with tempfile.TemporaryDirectory() as folder:
            r = await run(MockModel(), "计算", handler=broken, log_dir=folder)
            text = Path(r["log_path"]).read_text()
        self.assertEqual(r["status"], "failed")
        self.assertIn("TOOL_ERROR", text)
        self.assertNotIn("secret-like", text)

    async def test_06_no_progress(self):
        model = MockModel([call(call_id=f"call_{i}") for i in range(4)])
        r = await run(model, "重复计算", log_dir=None)
        self.assertEqual((r["stop_reason"], r["model_calls"], r["tool_calls"]), ("no_progress", 3, 2))

    async def test_07_call_limit(self):
        r = await run(MockModel(), "计算", Limits(max_calls=1), log_dir=None)
        self.assertEqual((r["stop_reason"], r["model_calls"], r["status"]), ("call_limit", 1, "stopped"))

    async def test_08_time_limit(self):
        r = await run(MockModel(delay=0.05), "计算", Limits(max_seconds=0.01), log_dir=None)
        self.assertEqual((r["stop_reason"], r["tool_calls"]), ("time_limit", 0))
        self.assertIsNone(r["estimated_usd"])


class BoundaryChecks(unittest.IsolatedAsyncioTestCase):
    async def test_step_limit(self):
        r = await run(MockModel(), "计算", Limits(max_steps=1), log_dir=None)
        self.assertEqual(r["stop_reason"], "step_limit")

    async def test_refusal_and_incomplete(self):
        for reply, reason in [(Reply(refusal=True), "refusal"),
                              (Reply(status="incomplete", text="391"), "response_incomplete")]:
            reply.usage = {"input_tokens": 10, "output_tokens": 5}
            r = await run(MockModel([reply]), "计算", log_dir=None)
            self.assertEqual((r["status"], r["stop_reason"]), ("failed", reason))

    async def test_unknown_usage(self):
        r = await run(MockModel([Reply(text="你好")]), "问好", log_dir=None)
        self.assertEqual(r["stop_reason"], "usage_unknown")
        self.assertFalse(r["usage_complete"])

    async def test_duplicate_call_id(self):
        r = await run(MockModel([call(), call()]), "计算", log_dir=None)
        self.assertEqual((r["stop_reason"], r["tool_calls"]), ("invalid_call_id", 1))

    async def test_structured_output(self):
        for text, reason in [('{"answer":391,"explanation":"相乘"}', "final_answer"),
                             ('{"answer":"391","explanation":"相乘"}', "invalid_structured_output"),
                             ('{"answer":1e309,"explanation":"溢出"}', "invalid_structured_output")]:
            r = await run(MockModel([final(text)]), "计算", tools_enabled=False,
                          text_format=ANSWER_FORMAT, log_dir=None)
            self.assertEqual(r["stop_reason"], reason)

    async def test_money_reservation_before_request(self):
        model = MockModel()
        model.mode = "real"  # 只测试预留逻辑；仍是本地假对象，不会联网。
        r = await run(model, "计算", Limits(run_budget_usd=0.01, call_reserve_usd=0.02), log_dir=None)
        self.assertEqual((r["stop_reason"], r["model_calls"]), ("budget_limit", 0))
        self.assertEqual(model.inputs, [])

    async def test_reservation_retained_and_underestimate_detected(self):
        model = MockModel()
        model.mode = "real"
        limits = Limits(run_budget_usd=0.02, call_reserve_usd=0.02,
                        input_usd_per_million=1, output_usd_per_million=2)
        r = await run(model, "计算", limits, log_dir=None)
        self.assertEqual((r["stop_reason"], r["model_calls"], r["reserved_usd"]), ("budget_limit", 1, 0.02))
        limits.input_usd_per_million = 1000
        r = await run(model, "计算", limits, log_dir=None)
        self.assertEqual(r["stop_reason"], "cost_reservation_exceeded")

    async def test_network_exception_no_retry(self):
        class Broken(MockModel):
            async def respond(self, *args):
                raise ConnectionError("fake network failure")
        r = await run(Broken(), "计算", log_dir=None)
        self.assertEqual((r["stop_reason"], r["model_calls"]), ("model_error", 1))
        self.assertFalse(r["usage_complete"])

    async def test_context_limit(self):
        r = await run(MockModel(), "计算", Limits(max_history_bytes=1), log_dir=None)
        self.assertEqual((r["stop_reason"], r["model_calls"]), ("context_limit", 0))

    async def test_cancel(self):
        task = asyncio.create_task(run(MockModel(delay=1), "计算", log_dir=None))
        await asyncio.sleep(0.01)
        task.cancel()
        r = await task
        self.assertEqual(r["stop_reason"], "cancelled")

    async def test_openai_adapter_contract_with_fake_client(self):
        # 验证请求形状与完整 output 回放；不是供应商集成测试。
        item = {"type": "reasoning", "id": "rs_fake", "encrypted_content": "opaque", "summary": []}
        class Item:
            def model_dump(self, **kwargs):
                return item
        captured = {}
        async def create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output=[Item()], output_text="391", status="completed",
                                   usage=None, incomplete_details=None, id="resp_fake", model="fake")
        model = OpenAIModel.__new__(OpenAIModel)
        model.model = "fake"
        model.client = SimpleNamespace(responses=SimpleNamespace(create=create))
        reply = await model.respond([{"role": "user", "content": "计算"}], [TOOL], ANSWER_FORMAT, 1024)
        self.assertFalse(captured["store"])
        self.assertFalse(captured["parallel_tool_calls"])
        self.assertEqual(captured["text"]["format"], ANSWER_FORMAT)
        self.assertEqual(reply.output, [item])


class CalculatorBoundaries(unittest.TestCase):
    def test_all_operations(self):
        for op, expected in [("add", 10), ("subtract", 6), ("multiply", 16), ("divide", 4)]:
            self.assertEqual(calculator({"operation": op, "a": 8, "b": 2})["value"], expected)

    def test_invalid_json_unknown_tool_zero_and_range(self):
        self.assertEqual(execute("shell", "{}")["error"]["code"], "UNKNOWN_TOOL")
        for raw in ('{', '{"a":NaN}', '{"a":1,"a":2}', 'x' * 2049):
            self.assertEqual(execute("calculator", raw)["error"]["code"], "INVALID_JSON")
        self.assertEqual(calculator({"operation": "divide", "a": 1, "b": 0})["error"]["code"], "DIVISION_BY_ZERO")
        self.assertEqual(calculator({"operation": "add", "a": 1e13, "b": 0})["error"]["code"], "OUT_OF_RANGE")
        self.assertEqual(calculator({"operation": "add", "a": 1, "b": 0, "extra": 1})["error"]["code"], "INVALID_ARGUMENTS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
