"""D05、D06：宿主掌握循环、预算预留、执行权和日志。"""
import asyncio
import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from calculator import TOOL, calculator, execute, parse_arguments
from model import SYSTEM, DEVELOPER


@dataclass
class Limits:
    max_steps: int = 6
    max_calls: int = 4
    max_seconds: float = 30
    repeat_limit: int = 2
    max_history_bytes: int = 24000
    max_output_tokens: int = 1024
    run_budget_usd: float = 0
    call_reserve_usd: float = 0
    input_usd_per_million: float = 0
    output_usd_per_million: float = 0


def estimate(usage, limits):
    return (usage["input_tokens"] * limits.input_usd_per_million
            + usage["output_tokens"] * limits.output_usd_per_million) / 1_000_000


async def run(model, question, limits=None, log_dir="logs", tools_enabled=True,
              text_format=None, developer=DEVELOPER, handler=calculator):
    limits = limits or Limits()
    start = time.monotonic()
    run_id = uuid.uuid4().hex
    events = []
    calls = tool_calls = steps = 0
    used_in = used_out = 0
    estimated = reserved = 0.0
    usage_complete = True
    seen, seen_ids = set(), set()
    stale = 0
    history = [{"role": "system", "content": SYSTEM},
               {"role": "developer", "content": developer},
               {"role": "user", "content": question}]

    def log(event, **fields):
        events.append({"run_id": run_id, "event": event,
                       "elapsed_ms": round((time.monotonic() - start) * 1000, 3),
                       **fields})

    def finish(reason, answer="", status="stopped"):
        result = {"run_id": run_id, "mode": model.mode, "status": status,
                  "stop_reason": reason, "answer": answer, "steps": steps,
                  "model_calls": calls, "tool_calls": tool_calls,
                  "input_tokens": used_in, "output_tokens": used_out,
                  "usage_complete": usage_complete,
                  "estimated_usd": round(estimated, 8) if usage_complete else None,
                  "reserved_usd": round(reserved, 8),
                  "elapsed_ms": round((time.monotonic() - start) * 1000, 3)}
        log("run_end", **{k: v for k, v in result.items()
                          if k not in ("run_id", "elapsed_ms")})
        if log_dir is not None:
            folder = Path(log_dir)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{run_id}.jsonl"
            with path.open("x", encoding="utf-8") as file:
                for event in events:
                    file.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
            result["log_path"] = str(path.resolve())
        return result

    log("run_start", mode=model.mode, utc=datetime.now(timezone.utc).isoformat(),
        prompt_sha256=hashlib.sha256(question.encode()).hexdigest())
    while True:
        remaining = limits.max_seconds - (time.monotonic() - start)
        if remaining <= 0:
            return finish("time_limit")
        if steps >= limits.max_steps:
            return finish("step_limit")
        if calls >= limits.max_calls:
            return finish("call_limit")
        if len(json.dumps(history, ensure_ascii=False).encode()) > limits.max_history_bytes:
            return finish("context_limit")
        if model.mode == "real":
            if (limits.call_reserve_usd <= 0 or
                    reserved + limits.call_reserve_usd > limits.run_budget_usd + 1e-12):
                return finish("budget_limit")
            # 本周不释放预留：失败、超时、成功请求都占用一次额度。
            reserved += limits.call_reserve_usd
        steps += 1
        calls += 1
        log("model_start", step=steps, call_number=calls)
        try:
            reply = await asyncio.wait_for(
                model.respond(history, [TOOL] if tools_enabled else [],
                              text_format, limits.max_output_tokens),
                timeout=remaining)
        except asyncio.TimeoutError:
            usage_complete = False
            log("model_error", code="TIMEOUT", billing="unknown")
            return finish("time_limit")
        except asyncio.CancelledError:
            usage_complete = False
            return finish("cancelled")
        except Exception as exc:
            usage_complete = False
            log("model_error", code=type(exc).__name__, billing="unknown")
            return finish("model_error", status="failed")
        log("model_response", response_id=reply.response_id, model=reply.model,
            api_status=reply.status, usage=reply.usage,
            refusal=reply.refusal, incomplete_reason=reply.incomplete_reason)
        valid_usage = (isinstance(reply.usage, dict) and all(
            type(reply.usage.get(k)) is int and reply.usage[k] >= 0
            for k in ("input_tokens", "output_tokens")))
        if valid_usage:
            used_in += reply.usage["input_tokens"]
            used_out += reply.usage["output_tokens"]
            cost = estimate(reply.usage, limits) if model.mode == "real" else 0
            estimated += cost
            if model.mode == "real" and cost > limits.call_reserve_usd:
                return finish("cost_reservation_exceeded", status="failed")
        else:
            usage_complete = False
            return finish("usage_unknown")
        if time.monotonic() - start >= limits.max_seconds:
            return finish("time_limit")
        if reply.refusal:
            return finish("refusal", status="failed")
        if reply.status != "completed":
            return finish("response_" + reply.status, status="failed")
        requests = [item for item in reply.output if item.get("type") == "function_call"]
        if not requests:
            if not reply.text.strip():
                return finish("empty_response", status="failed")
            if text_format:
                try:
                    data = parse_arguments(reply.text)
                    valid = (isinstance(data, dict)
                             and set(data) == {"answer", "explanation"}
                             and type(data["answer"]) in (int, float)
                             and math.isfinite(data["answer"])
                             and isinstance(data["explanation"], str))
                    if not valid:
                        raise ValueError("输出不满足约定结构")
                except (ValueError, TypeError, OverflowError, RecursionError):
                    return finish("invalid_structured_output", status="failed")
            # completed 只指协议正常收尾；答案质量交给独立验收。
            return finish("final_answer", reply.text, status="completed")
        if not tools_enabled or len(requests) != 1:
            return finish("unexpected_tool_batch", status="failed")
        request = requests[0]
        call_id = request.get("call_id")
        name, raw = request.get("name"), request.get("arguments")
        if not isinstance(call_id, str) or not call_id or call_id in seen_ids:
            return finish("invalid_call_id", status="failed")
        seen_ids.add(call_id)
        try:
            canonical = json.dumps(parse_arguments(raw), sort_keys=True, allow_nan=False)
        except (ValueError, RecursionError):
            canonical = str(raw)
        fingerprint = (str(name), canonical)
        stale = stale + 1 if fingerprint in seen else 0
        seen.add(fingerprint)
        log("tool_request", call_id=call_id, name=name, arguments=raw)
        if stale >= limits.repeat_limit:
            log("tool_blocked", call_id=call_id, reason="no_progress")
            return finish("no_progress")
        history.extend(reply.output)
        tool_start = time.monotonic()
        result = execute(name, raw, handler=handler)
        tool_calls += 1
        log("tool_result", call_id=call_id, tool_status="ok" if result["ok"] else "error",
            result=result, tool_elapsed_ms=round((time.monotonic() - tool_start) * 1000, 3))
        history.append({"type": "function_call_output", "call_id": call_id,
                        "output": json.dumps(result, ensure_ascii=False, allow_nan=False)})
        # 第一周选择遇工具错误即终止；不让后续措辞掩盖失败。
        if not result["ok"]:
            return finish("tool_error", status="failed")
