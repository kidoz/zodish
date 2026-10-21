"""Microbenchmarks for Zodish asynchronous parsing.

Run from a checkout with the package installed:

    python benchmarks/bench_async.py
    python benchmarks/bench_async.py --json
    python benchmarks/bench_async.py --smoke

The harness creates event loops outside timed iterations, warms schemas, and
reports medians over repeated samples. It has no pass/fail thresholds because
scheduling overhead depends on the host and interpreter.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import statistics
import time
import tracemalloc
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from zodish import Schema, z


def _outcome(result: Any) -> bool:
    if hasattr(result, "success"):
        return bool(result.success)
    return True


@dataclass(frozen=True)
class Scenario:
    name: str
    schema: Schema[Any, Any]
    payload: Any
    iterations: int
    callback_count: int = 0
    concurrent: int = 1
    expect_failure: bool = False
    inspect_issues: bool = False

    def call(self) -> Any:
        if self.expect_failure:
            if self.inspect_issues:
                result = self.schema.safe_parse_async(self.payload)
                return self._inspect(result)
            return self.schema.safe_parse_async(self.payload)
        return self.schema.parse_async(self.payload)

    @staticmethod
    async def _inspect(result: Any) -> Any:
        outcome = await result
        return None if outcome.success else outcome.error.issues

    @staticmethod
    async def _gather(schema: Schema[Any, Any], payload: Any, count: int) -> Any:
        return await asyncio.gather(
            *(schema.parse_async(payload) for _ in range(count)),
        )


@dataclass(frozen=True)
class SyncScenario:
    name: str
    schema: Schema[Any, Any]
    payload: Any
    iterations: int
    expect_failure: bool = False

    def call(self) -> Any:
        if self.expect_failure:
            return self.schema.safe_parse(self.payload)
        return self.schema.parse(self.payload)


def _callbacks(*, sleep: bool) -> tuple[Callable[..., Any], ...]:
    async def positive(value: int) -> bool:
        if sleep:
            await asyncio.sleep(0)
        return value > 0

    async def nonempty(value: str) -> bool:
        if sleep:
            await asyncio.sleep(0)
        return bool(value)

    return positive, nonempty


def scenarios() -> list[Scenario | SyncScenario]:
    positive, nonempty = _callbacks(sleep=False)
    slow_positive, slow_nonempty = _callbacks(sleep=True)

    sync_user = z.object({
        "id": z.integer().positive(),
        "name": z.string().min(1).max(64),
        "tags": z.string().array(),
    })
    sync_payload = {"id": 1, "name": "Alex", "tags": ["admin", "staff"]}

    immediate = z.object({
        "id": z.integer().refine_async(positive),
        "name": z.string().refine_async(nonempty),
        "tags": z.string().refine_async(nonempty).array(),
    })
    immediate_payload = {"id": 1, "name": "Alex", "tags": ["admin", "staff"]}

    suspended = z.object({
        "id": z.integer().refine_async(slow_positive),
        "name": z.string().refine_async(slow_nonempty),
        "tags": z.string().refine_async(slow_nonempty).array(),
    })

    sequential = z.object({
        f"field_{index}": z.integer().refine_async(slow_positive)
        for index in range(8)
    })
    sequential_payload = {f"field_{index}": 1 for index in range(8)}

    failing = z.object({
        "id": z.integer().refine_async(positive),
        "name": z.string().refine_async(nonempty),
        "tags": z.string().refine_async(nonempty).array(),
    })
    failing_payload = {"id": 0, "name": "", "tags": ["ok", ""]}

    return [
        SyncScenario("sync_baseline", sync_user, sync_payload, 5_000),
        Scenario("sync_graph", sync_user, sync_payload, 5_000),
        Scenario("immediate_coroutine", immediate, immediate_payload, 5_000, 4),
        Scenario("sleep_zero", suspended, immediate_payload, 2_000, 4),
        Scenario("sequential_callbacks", sequential, sequential_payload, 1_000, 8),
        Scenario(
            "nested_failures", failing, failing_payload, 2_000, 4,
            expect_failure=True,
        ),
        Scenario(
            "nested_failures_issues", failing, failing_payload, 2_000, 4,
            expect_failure=True, inspect_issues=True,
        ),
        Scenario(
            "concurrent_parses", suspended, immediate_payload, 200, 4, concurrent=16,
        ),
    ]


async def _check_async(scenario: Scenario) -> None:
    result = scenario.call()
    if scenario.name == "concurrent_parses":
        values = await result
        if len(values) != scenario.concurrent:
            raise RuntimeError(f"{scenario.name}: wrong concurrent result count")
        return
    if scenario.inspect_issues:
        if not await result:
            raise RuntimeError(f"{scenario.name}: expected failure")
        return
    outcome = await result
    if _outcome(outcome) == scenario.expect_failure:
        expectation = "failure" if scenario.expect_failure else "success"
        raise RuntimeError(f"{scenario.name}: expected {expectation}")


def _check(scenario: Scenario | SyncScenario) -> None:
    if isinstance(scenario, SyncScenario):
        result = scenario.call()
        if _outcome(result) == scenario.expect_failure:
            expectation = "failure" if scenario.expect_failure else "success"
            raise RuntimeError(f"{scenario.name}: expected {expectation}")
        return
    if scenario.name == "concurrent_parses":
        loop = asyncio.new_event_loop()
        try:
            values = loop.run_until_complete(
                Scenario._gather(scenario.schema, scenario.payload, scenario.concurrent),
            )
            if len(values) != scenario.concurrent:
                raise RuntimeError(
                    f"{scenario.name}: wrong concurrent result count",
                )
        finally:
            loop.close()
        return
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_check_async(scenario))
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _run(scenario: Scenario | SyncScenario, *, loop: asyncio.AbstractEventLoop | None) -> Any:
    if isinstance(scenario, SyncScenario):
        return scenario.call()
    if scenario.name == "concurrent_parses":
        return loop.run_until_complete(
            Scenario._gather(scenario.schema, scenario.payload, scenario.concurrent),
        )
    return loop.run_until_complete(scenario.call())


def measure(
    scenario: Scenario | SyncScenario,
    *,
    iterations: int,
    repeat: int,
    memory: bool,
) -> dict[str, Any]:
    _check(scenario)
    loop = asyncio.new_event_loop() if isinstance(scenario, Scenario) else None
    try:
        for _ in range(min(iterations, 50 if loop is not None else 100)):
            _run(scenario, loop=loop)

        samples = []
        for _ in range(repeat):
            started = time.perf_counter()
            for _ in range(iterations):
                _run(scenario, loop=loop)
            samples.append(time.perf_counter() - started)
    finally:
        if loop is not None:
            loop.close()

    elapsed = statistics.median(samples)
    sample_microseconds = [
        sample / iterations * 1_000_000 for sample in samples
    ]
    deviation = (
        statistics.stdev(sample_microseconds)
        if len(sample_microseconds) > 1
        else 0.0
    )
    operations_per_second = iterations / elapsed
    retained = peak = None
    if memory:
        memory_loop = asyncio.new_event_loop() if isinstance(scenario, Scenario) else None
        gc.collect()
        tracemalloc.start()
        try:
            _run(scenario, loop=memory_loop)
            current, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            if memory_loop is not None:
                memory_loop.close()
        retained = current
        peak = peak_bytes

    concurrency = getattr(scenario, "concurrent", 1)
    callbacks = getattr(scenario, "callback_count", 0) * concurrency
    return {
        "name": scenario.name,
        "iterations": iterations,
        "repeats": repeat,
        "operations_per_second": operations_per_second,
        "microseconds_per_operation": elapsed / iterations * 1_000_000,
        "microseconds_stddev": deviation,
        "microseconds_min": min(sample_microseconds),
        "microseconds_max": max(sample_microseconds),
        "sample_microseconds_per_operation": sample_microseconds,
        "callbacks_per_operation": callbacks,
        "callbacks_per_second": operations_per_second * callbacks,
        "concurrency": concurrency,
        "parses_per_operation": concurrency,
        "parses_per_second": operations_per_second * concurrency,
        "expect_failure": getattr(scenario, "expect_failure", False),
        "mode": "sync_parse" if isinstance(scenario, SyncScenario) else "parse_async",
        "retained_bytes": retained,
        "peak_bytes": peak,
    }


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=_positive_int, help="override per-scenario counts")
    parser.add_argument("--repeat", type=_positive_int, help="timed repetitions per scenario")
    parser.add_argument("--filter", help="only run scenarios whose name contains this text")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--smoke", action="store_true", help="use minimal counts for CI")
    parser.add_argument(
        "--memory",
        action="store_true",
        help="record retained and peak tracemalloc bytes after one operation",
    )
    args = parser.parse_args(argv)

    repeat = args.repeat if args.repeat is not None else (1 if args.smoke else 5)
    selected = [
        scenario
        for scenario in scenarios()
        if not args.filter or args.filter in scenario.name
    ]
    if not selected:
        parser.error(f"no scenario matches {args.filter!r}")

    results = []
    for scenario in selected:
        iterations = args.iterations if args.iterations is not None else scenario.iterations
        if args.smoke:
            iterations = min(iterations, 10)
        results.append(
            measure(
                scenario,
                iterations=iterations,
                repeat=repeat,
                memory=args.memory,
            )
        )

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
        return 0

    print(f"Zodish async benchmarks (median of {repeat} run{'s' if repeat != 1 else ''})")
    print(
        f"{'scenario':<24} {'ops/s':>12} {'us/op':>10} {'sd':>8} "
        f"{'callbacks/s':>14} {'parses/s':>14}",
    )
    print("-" * 92)
    for result in results:
        print(
            f"{result['name']:<24} "
            f"{result['operations_per_second']:>12,.0f} "
            f"{result['microseconds_per_operation']:>10.3f} "
            f"{result['microseconds_stddev']:>8.3f} "
            f"{result['callbacks_per_second']:>14,.0f} "
            f"{result['parses_per_second']:>14,.0f}"
        )
        if args.memory:
            print(
                f"{'':<24} retained={result['retained_bytes']:,} B "
                f"peak={result['peak_bytes']:,} B",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
