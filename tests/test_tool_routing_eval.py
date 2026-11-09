"""板端 LLM 原生 MCP 路由评测；mock 模式不产生设备副作用。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp_sse_client import build_mcp_pool_from_env
from spacemit_llm import LLMModel


def _normalized_calls(calls: list[dict]) -> list[dict]:
    return [
        {
            "name": str(call.get("name") or ""),
            "arguments": call.get("arguments") or {},
        }
        for call in calls
    ]


def _has_side_effect(case: dict) -> bool:
    return any(
        not str(call.get("name") or "").startswith("get_")
        for call in case.get("expected_calls") or []
    )


async def _call_mcp(
    pool, name: str, arguments: Optional[dict] = None
) -> str:
    result = await pool.call_tool(name, arguments or {})
    if not result.ok:
        raise RuntimeError(result.error or f"{name} 调用失败")
    return result.text or "ok"


async def _prepare_side_effect(pool, spec: dict) -> tuple[str, str]:
    state_tool = str(spec["state_tool"])
    initial = await _call_mcp(pool, state_tool)
    expected = str(spec["expected_state"])
    restore_map = spec.get("restore_by_state") or {}
    opposite = next(
        (
            (state, tool)
            for state, tool in restore_map.items()
            if str(state) != expected
        ),
        None,
    )
    if opposite is None:
        raise ValueError("side_effect 缺少相反状态的准备工具")
    await _call_mcp(pool, str(opposite[1]))
    baseline = await _call_mcp(pool, state_tool)
    if str(opposite[0]) not in baseline:
        raise AssertionError(f"准备状态失败: 期望 {opposite[0]!r}, 实际 {baseline!r}")
    return initial, baseline


async def _restore_side_effect(pool, spec: dict, initial: str) -> None:
    for state, tool in (spec.get("restore_by_state") or {}).items():
        if str(state) in initial:
            await _call_mcp(pool, str(tool))
            restored = await _call_mcp(pool, str(spec["state_tool"]))
            if str(state) not in restored:
                raise AssertionError(
                    f"恢复状态失败: 期望 {state!r}, 实际 {restored!r}"
                )
            return
    raise AssertionError(f"无法从初始状态确定恢复动作: {initial!r}")


async def evaluate(args) -> int:
    data = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = data.get("cases") or []
    model = LLMModel()
    pool = None
    tools: list[Any] = data.get("tools") or []

    if args.mode == "live":
        pool = build_mcp_pool_from_env()
        await pool.start()
        if not await pool.wait_ready(timeout=args.ready_timeout):
            await pool.stop()
            raise ConnectionError("MCP Server 在规定时间内未就绪")
        tools = pool.list_cached_tools()

    report = {
        "mode": args.mode,
        "model": model._model_path,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tool_names": [
            tool.get("name") if isinstance(tool, dict) else tool.name
            for tool in tools
        ],
        "cases": [],
    }
    loop = asyncio.get_running_loop()

    try:
        for case in cases:
            if (
                args.mode == "live"
                and _has_side_effect(case)
                and not args.allow_side_effects
            ):
                report["cases"].append(
                    {
                        "id": case["id"],
                        "status": "skipped",
                        "reason": "需要 --allow-side-effects",
                    }
                )
                continue

            actual_executor_calls: list[dict] = []
            initial_state = ""
            baseline_state = ""
            side_effect = case.get("side_effect")
            if args.mode == "live" and side_effect:
                initial_state, baseline_state = await _prepare_side_effect(
                    pool, side_effect
                )

            def execute(name: str, arguments: dict) -> str:
                actual_executor_calls.append(
                    {"name": name, "arguments": arguments or {}}
                )
                if args.mode == "mock":
                    return str(
                        (case.get("mock_results") or {}).get(name, "ok")
                    )
                future = asyncio.run_coroutine_threadsafe(
                    pool.call_tool(name, arguments or {}),
                    loop,
                )
                result = future.result(timeout=args.call_timeout)
                if not result.ok:
                    return f"ERROR: {result.error or 'tools/call failed'}"
                return result.text or "ok"

            started = time.perf_counter()
            error = ""
            result: dict = {"calls": [], "content": "", "rounds": 0}
            final_state = ""
            restored = False
            try:
                result = await loop.run_in_executor(
                    None,
                    lambda: model.run_tool_agent(
                        str(case["prompt"]), tools, execute
                    ),
                )
                if args.mode == "live" and side_effect:
                    final_state = await _call_mcp(
                        pool, str(side_effect["state_tool"])
                    )
                    expected_state = str(side_effect["expected_state"])
                    if expected_state not in final_state:
                        raise AssertionError(
                            f"设备终态错误: 期望 {expected_state!r}, "
                            f"实际 {final_state!r}"
                        )
                    if final_state == baseline_state:
                        raise AssertionError("设备状态未发生变化")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            finally:
                if args.mode == "live" and side_effect and initial_state:
                    try:
                        await _restore_side_effect(
                            pool, side_effect, initial_state
                        )
                        restored = True
                    except Exception as exc:
                        restore_error = f"恢复失败: {type(exc).__name__}: {exc}"
                        error = f"{error}; {restore_error}".strip("; ")

            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            actual = _normalized_calls(result.get("calls") or [])
            expected = _normalized_calls(case.get("expected_calls") or [])
            passed = not error and actual == expected
            row = {
                "id": case["id"],
                "prompt": case["prompt"],
                "status": "passed" if passed else "failed",
                "expected_calls": expected,
                "actual_calls": actual,
                "executor_calls": actual_executor_calls,
                "rounds": result.get("rounds"),
                "reply": result.get("content") or "",
                "elapsed_ms": elapsed_ms,
            }
            if error:
                row["error"] = error
            if side_effect and args.mode == "live":
                row.update(
                    {
                        "initial_state": initial_state,
                        "baseline_state": baseline_state,
                        "final_state": final_state,
                        "restored": restored,
                    }
                )
            report["cases"].append(row)
            print(
                f"[{'PASS' if passed else 'FAIL'}] {case['id']} "
                f"{elapsed_ms:.1f}ms expected={expected} actual={actual}"
            )

    finally:
        if pool is not None:
            await pool.stop()

    evaluated = [
        row for row in report["cases"] if row.get("status") != "skipped"
    ]
    passed_count = sum(row["status"] == "passed" for row in evaluated)
    accuracy = passed_count / len(evaluated) if evaluated else 0.0
    report["summary"] = {
        "evaluated": len(evaluated),
        "passed": passed_count,
        "failed": len(evaluated) - passed_count,
        "skipped": len(report["cases"]) - len(evaluated),
        "accuracy": accuracy,
        "min_accuracy": args.min_accuracy,
    }
    print(
        f"完成: {passed_count}/{len(evaluated)} "
        f"accuracy={accuracy:.1%} threshold={args.min_accuracy:.1%}"
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"报告: {args.report}")
    return 0 if evaluated and accuracy >= args.min_accuracy else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("mock", "live"), default="mock")
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path(__file__).with_name("tool_routing_cases.json"),
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--min-accuracy", type=float, default=0.8)
    parser.add_argument("--ready-timeout", type=float, default=15.0)
    parser.add_argument("--call-timeout", type=float, default=20.0)
    parser.add_argument("--allow-side-effects", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(evaluate(args)))


if __name__ == "__main__":
    main()
