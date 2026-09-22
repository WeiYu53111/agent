"""D03：只有固定四则运算，不接收或执行任意表达式。"""
import json
import math
import operator

OPS = {"add": operator.add, "subtract": operator.sub,
       "multiply": operator.mul, "divide": operator.truediv}

TOOL = {
    "type": "function", "name": "calculator",
    "description": "对两个有限数值做四则运算；需要计算时使用此工具。",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": list(OPS)},
            "a": {"type": "number"}, "b": {"type": "number"},
        },
        "required": ["operation", "a", "b"],
        "additionalProperties": False,
    },
}


def error(code, message):
    return {"ok": False, "value": None,
            "error": {"code": code, "message": message}}


def calculator(args):
    if not isinstance(args, dict) or set(args) != {"operation", "a", "b"}:
        return error("INVALID_ARGUMENTS", "必须且只能包含 operation、a、b")
    op, a, b = args["operation"], args["a"], args["b"]
    if not isinstance(op, str) or op not in OPS:
        return error("INVALID_OPERATION", "仅支持 add/subtract/multiply/divide")
    # bool 是 int 的子类，因此不能仅用 isinstance(x, (int, float))。
    for value in (a, b):
        if type(value) not in (int, float):
            return error("INVALID_TYPE", "a、b 必须是数值，不能是字符串或布尔值")
        if abs(value) > 1e12 or not math.isfinite(value):
            return error("OUT_OF_RANGE", "输入必须有限且绝对值不超过 10^12")
    if op == "divide" and b == 0:
        return error("DIVISION_BY_ZERO", "除数不能为零")
    value = OPS[op](a, b)
    return {"ok": True, "value": value, "error": None}


def parse_arguments(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 2048:
        raise ValueError("参数不是字符串或超过 2048 字节")

    def reject_constant(value):
        raise ValueError("不允许 NaN/Infinity")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON 不允许重复键")
            result[key] = value
        return result

    return json.loads(raw, parse_constant=reject_constant,
                      object_pairs_hook=unique_object)


def execute(name, raw, handler=calculator):
    if name != "calculator":
        return error("UNKNOWN_TOOL", "未注册的工具")
    try:
        args = parse_arguments(raw)
    except (ValueError, RecursionError):
        return error("INVALID_JSON", "工具参数不是允许的 JSON")
    try:
        return handler(args)
    except Exception:
        # 不把 traceback、环境变量等内部信息交给模型。
        return error("TOOL_ERROR", "工具内部执行失败")
