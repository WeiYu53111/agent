"""统一入口：默认模拟；只有 --real 才会创建 API 客户端。"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import platform
from dataclasses import asdict
from pathlib import Path

from model import (ANSWER_FORMAT, DEVELOPER, QUESTION, MockModel, OpenAIModel,
                   Reply, call, final)
from runtime import Limits, run


def read_budget(path, limits, run_count, evaluation):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    keys = ("month_budget_usd", "month_remaining_usd", "eval_budget_usd",
            "eval_remaining_usd", "run_budget_usd", "call_reserve_usd",
            "input_usd_per_million", "output_usd_per_million")
    for key in keys:
        value = config.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"请先在 {path} 填写正数：{key}")
    for scope in ("month", "eval"):
        if config[f"{scope}_remaining_usd"] > config[f"{scope}_budget_usd"]:
            raise ValueError(f"{scope} 的剩余额度不能大于总预算")
    if config.get("price_checked_on") in (None, "", "待填写"):
        raise ValueError("请记录价格核对日期 price_checked_on")
    for key in ("run_budget_usd", "call_reserve_usd",
                "input_usd_per_million", "output_usd_per_million"):
        setattr(limits, key, config[key])
    possible_calls = min(limits.max_calls, limits.max_steps,
                         math.floor((limits.run_budget_usd + 1e-12)
                                    / limits.call_reserve_usd))
    planned = run_count * possible_calls * limits.call_reserve_usd
    if planned > config["month_remaining_usd"] + 1e-12:
        raise ValueError("本批次计划预留超过本月剩余额度")
    if evaluation and planned > config["eval_remaining_usd"] + 1e-12:
        raise ValueError("本批次计划预留超过评测剩余额度")
    return config


def mock_for(command, scenario, structured):
    if command != "run":
        text = (json.dumps({"answer": 391, "explanation": "17 × 23 = 391"},
                           ensure_ascii=False) if structured else "17 × 23 = 391。")
        return MockModel([final(text)])
    if scenario == "repeat":
        return MockModel([call(call_id=f"call_{i}") for i in range(20)])
    if scenario == "bad-arguments":
        return MockModel([call({"operation": "multiply", "a": "17", "b": 23})])
    if scenario == "refusal":
        return MockModel([Reply(refusal=True, usage={"input_tokens": 10, "output_tokens": 5})])
    if scenario == "incomplete":
        return MockModel([Reply(status="incomplete", text="17 ×",
                                incomplete_reason="max_output_tokens",
                                usage={"input_tokens": 10, "output_tokens": 5})])
    return MockModel()


async def main(args):
    limits = Limits(max_steps=args.max_steps, max_calls=args.max_calls,
                    max_seconds=args.max_seconds)
    trials = [(False, 1)] if args.command != "compare" else [
        (structured, trial) for structured in (False, True) for trial in range(1, 4)]
    if args.command != "run":
        limits.max_calls = limits.max_steps = 1
    if args.real and args.scenario != "normal":
        raise ValueError("故障场景只支持模拟模式")
    config = read_budget(args.config, limits, len(trials), args.command == "compare") if args.real else {}
    try:
        sdk_version = importlib.metadata.version("openai")
    except importlib.metadata.PackageNotFoundError:
        sdk_version = "not-installed (mock needs no SDK)"
    records = []
    for structured, trial in trials:
        model = OpenAIModel() if args.real else mock_for(args.command, args.scenario, structured)
        try:
            result = await run(
                model, args.question, limits, tools_enabled=args.command == "run",
                text_format=ANSWER_FORMAT if structured else None,
                developer=args.developer)
        finally:
            await model.close()
        result.update(format="structured" if structured else "plain", trial=trial)
        records.append(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        # 额度未知、接口失败或未完成时，不自动继续整批请求。
        if result["status"] != "completed":
            break
    folder = Path("artifacts")
    folder.mkdir(exist_ok=True)
    manifest = {"python": platform.python_version(), "sdk": sdk_version,
                "mode": "real" if args.real else "mock", "limits": asdict(limits),
                "config": config, "question": args.question, "developer": args.developer,
                "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in sorted(Path(__file__).parent.glob("*.py"))},
                "results": records}
    path = folder / f"{records[0]['run_id']}-manifest.json"
    with path.open("x", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, indent=2)
    print(f"实验清单：{path.resolve()}")
    return 0 if all(r["status"] == "completed" for r in records) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["probe", "compare", "run"])
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--question", default=QUESTION)
    parser.add_argument("--developer", default=DEVELOPER)
    parser.add_argument("--scenario", choices=["normal", "repeat", "bad-arguments", "refusal", "incomplete"], default="normal")
    parser.add_argument("--max-calls", type=int, default=4)
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--max-seconds", type=float, default=30)
    args = parser.parse_args()
    if args.max_calls <= 0 or args.max_steps <= 0 or not math.isfinite(args.max_seconds) or args.max_seconds <= 0:
        parser.error("次数和时长限制必须为正数")
    try:
        raise SystemExit(asyncio.run(main(args)))
    except ValueError as exc:
        parser.exit(2, f"配置错误：{exc}\n")
    except (OSError, ImportError) as exc:
        parser.exit(2, f"环境错误：{type(exc).__name__}；请核对文件路径与 SDK 安装。\n")
