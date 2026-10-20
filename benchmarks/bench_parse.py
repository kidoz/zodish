"""Microbenchmarks for Zodish parsing.

Run from a checkout with the package installed:

    python benchmarks/bench_parse.py
    python benchmarks/bench_parse.py --json
    python benchmarks/bench_parse.py --smoke

The harness reports median throughput over several repetitions. It has no
pass/fail thresholds because results depend on the host, Python version, and
native build profile.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from zodish import Schema, z


def _inspect_failure(result: Any) -> Any:
    """Return materialized issues without retaining the failure in this frame."""
    return None if result.success else result.error.issues


@dataclass(frozen=True)
class Scenario:
    name: str
    schema: Schema[Any, Any]
    payload: Any
    iterations: int
    expect_failure: bool = False
    items_per_operation: int = 1
    inspect_issues: bool = False

    def call(self) -> Any:
        if self.expect_failure:
            if self.inspect_issues:
                return _inspect_failure(self.schema.safe_parse(self.payload))
            return self.schema.safe_parse(self.payload)
        return self.schema.parse(self.payload)

    def check(self) -> None:
        result = self.schema.safe_parse(self.payload)
        if result.success == self.expect_failure:
            expectation = "failure" if self.expect_failure else "success"
            raise RuntimeError(f"{self.name}: expected {expectation}")


def scenarios() -> list[Scenario]:
    def tree(depth: int) -> dict[str, Any]:
        children = [tree(depth - 1), tree(depth - 1)] if depth else []
        return {"value": depth, "children": children}

    user = z.object(
        {
            "id": z.integer().positive(),
            "name": z.string().min(1).max(64),
            "tags": z.string().array(),
        }
    )
    branches = [
        z.object({"kind": z.literal(kind), "value": z.integer()})
        for kind in ("created", "updated", "deleted", "archived")
    ]
    recursive: Schema[Any, Any] = z.lazy(
        lambda: z.object({"value": z.integer(), "children": recursive.array()}),
    )
    return [
        Scenario("string_success", z.string().min(3).max(32), "hello", 20_000),
        Scenario(
            "object_success",
            user,
            {"id": 1, "name": "Alex", "tags": ["admin", "staff"]},
            5_000,
        ),
        Scenario(
            "object_failure",
            user,
            {"id": -1, "name": "", "tags": ["admin", 1]},
            5_000,
            expect_failure=True,
        ),
        Scenario(
            "object_failure_issues",
            user,
            {"id": -1, "name": "", "tags": ["admin", 1]},
            5_000,
            expect_failure=True,
            inspect_issues=True,
        ),
        Scenario(
            "array_1000",
            z.integer().array(),
            list(range(1_000)),
            100,
            items_per_operation=1_000,
        ),
        Scenario(
            "union_first",
            z.union(*branches),
            {"kind": "created", "value": 1},
            5_000,
        ),
        Scenario(
            "union_last",
            z.union(*branches),
            {"kind": "archived", "value": 1},
            5_000,
        ),
        Scenario("recursive_tree", recursive, tree(4), 1_000),
        Scenario(
            "refine_callback",
            z.integer().refine(lambda value: value > 0),
            1,
            5_000,
        ),
    ]


def measure(scenario: Scenario, *, iterations: int, repeat: int) -> dict[str, Any]:
    scenario.check()
    call = scenario.call
    for _ in range(min(iterations, 100)):
        call()

    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        for _ in range(iterations):
            call()
        samples.append(time.perf_counter() - started)

    elapsed = statistics.median(samples)
    operations_per_second = iterations / elapsed
    return {
        "name": scenario.name,
        "iterations": iterations,
        "repeats": repeat,
        "operations_per_second": operations_per_second,
        "microseconds_per_operation": elapsed / iterations * 1_000_000,
        "items_per_operation": scenario.items_per_operation,
        "items_per_second": operations_per_second * scenario.items_per_operation,
        "expect_failure": scenario.expect_failure,
        "parse_mode": (
            "safe_parse+issues"
            if scenario.inspect_issues
            else "safe_parse"
            if scenario.expect_failure
            else "parse"
        ),
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
    args = parser.parse_args(argv)

    repeat = args.repeat if args.repeat is not None else (1 if args.smoke else 5)
    selected = [
        scenario for scenario in scenarios() if not args.filter or args.filter in scenario.name
    ]
    if not selected:
        parser.error(f"no scenario matches {args.filter!r}")

    results = []
    for scenario in selected:
        iterations = args.iterations if args.iterations is not None else scenario.iterations
        if args.smoke:
            iterations = min(iterations, 50)
        results.append(measure(scenario, iterations=iterations, repeat=repeat))

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
        return 0

    print(f"Zodish parse benchmarks (median of {repeat} run{'s' if repeat != 1 else ''})")
    print(f"{'scenario':<18} {'ops/s':>14} {'us/op':>10} {'items/s':>14}")
    print("-" * 60)
    for result in results:
        print(
            f"{result['name']:<18} "
            f"{result['operations_per_second']:>14,.0f} "
            f"{result['microseconds_per_operation']:>10.2f} "
            f"{result['items_per_second']:>14,.0f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
