"""D01、D02、D04：统一响应形状；模拟模型只用于机制验证。"""
import asyncio
import copy
import json
import os
from dataclasses import dataclass, field

SYSTEM = "你是中文教学助手。简洁回答，不声称执行了没有执行的动作。"
DEVELOPER = "有 calculator 时，数值计算必须请求工具；工具失败时如实说明。"
QUESTION = "请计算 17 乘以 23，并用一句中文解释。"
ANSWER_FORMAT = {
    "type": "json_schema", "name": "calculation_answer", "strict": True,
    "schema": {
        "type": "object",
        "properties": {"answer": {"type": "number"},
                       "explanation": {"type": "string"}},
        "required": ["answer", "explanation"],
        "additionalProperties": False,
    },
}


@dataclass
class Reply:
    status: str = "completed"
    text: str = ""
    output: list = field(default_factory=list)
    usage: dict | None = None
    refusal: bool = False
    response_id: str = "mock-response"
    model: str = "scripted-mock-v1"
    incomplete_reason: str | None = None


def final(text="391"):
    return Reply(text=text, usage={"input_tokens": 40, "output_tokens": 10})


def call(args=None, call_id="call_1", name="calculator"):
    args = args if args is not None else {"operation": "multiply", "a": 17, "b": 23}
    return Reply(output=[{"type": "function_call", "call_id": call_id,
                          "name": name, "arguments": json.dumps(args)}],
                 usage={"input_tokens": 50, "output_tokens": 20})


class MockModel:
    mode = "mock"

    def __init__(self, replies=None, delay=0):
        self.replies = replies or [call(), final()]
        self.delay = delay
        self.inputs = []
        self.index = 0

    async def respond(self, history, tools, text_format, max_output_tokens):
        self.inputs.append(copy.deepcopy(history))
        await asyncio.sleep(self.delay)
        reply = self.replies[min(self.index, len(self.replies) - 1)]
        self.index += 1
        return copy.deepcopy(reply)

    async def close(self):
        pass


class OpenAIModel:
    mode = "real"

    def __init__(self):
        from openai import AsyncOpenAI
        self.model = os.environ.get("OPENAI_MODEL", "")
        if not self.model or not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("真实模式需要 OPENAI_MODEL 与 OPENAI_API_KEY")
        base_url = os.environ.get(
        "OPENAI_BASE_URL",
        "https://api.deepseek.com",
        )
        self.client = AsyncOpenAI(max_retries=0, timeout=30.0,base_url=base_url)

    async def respond(self, history, tools, text_format, max_output_tokens):
        params = dict(model=self.model, input=history, store=False,
                      max_output_tokens=max_output_tokens,
                      include=["reasoning.encrypted_content"])
        if tools:
            params.update(tools=tools, parallel_tool_calls=False)
        if text_format:
            params["text"] = {"format": text_format}
        response = await self.client.responses.create(**params)
        # 完整保留 output，不能只保留 function_call 而丢掉推理状态项。
        output = [item.model_dump(exclude_none=True) for item in response.output]
        refusal = any(part.get("type") == "refusal"
                      for item in output if item.get("type") == "message"
                      for part in item.get("content", []))
        details = response.incomplete_details
        return Reply(
            status=response.status, text=response.output_text, output=output,
            usage=response.usage.model_dump() if response.usage else None,
            refusal=refusal, response_id=response.id, model=response.model,
            incomplete_reason=getattr(details, "reason", None),
        )

    async def close(self):
        await self.client.close()
