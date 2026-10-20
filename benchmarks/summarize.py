"""Render saved pyperf comparison results as a Markdown table."""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path
from typing import Any


LIBRARIES = ("zodish", "pydantic", "msgspec", "marshmallow")


def render(suite: Any) -> str:
    metadata = suite.get_metadata()
    rows: dict[str, dict[str, Any]] = {}
    for benchmark in suite.get_benchmarks():
        scenario, library = benchmark.get_name().split("/", 1)
        rows.setdefault(scenario, {})[library] = benchmark
    lines = [
        "# Local validation benchmark results",
        "",
        "Mean ± sample standard deviation in **microseconds per operation**; lower is better.",
        "This is a local measurement, not a cross-platform performance claim.",
        "Missing cells indicate unsupported scenarios, not zero cost.",
        "",
        f"- Python: {metadata.get('python_version', 'unknown')}",
        f"- Platform: {metadata.get('platform', 'unknown')}",
        f"- GIL enabled: {metadata.get('gil_enabled', 'unknown')}",
        f"- Zodish build: {metadata.get('zodish_build_profile', 'unknown')} (caller supplied)",
    ]
    for key, value in sorted(metadata.items()):
        if key.startswith("version_"):
            lines.append(f"- {key.removeprefix('version_')}: {value}")
    lines.extend([
        "",
        "| Scenario | Zodish | Pydantic | msgspec | Marshmallow |",
        "| --- | ---: | ---: | ---: | ---: |",
    ])
    noisy = []
    samples = set()
    for scenario, libraries in rows.items():
        cells = []
        for library in LIBRARIES:
            benchmark = libraries.get(library)
            if benchmark is None:
                cells.append("—")
                continue
            mean = benchmark.mean()
            count = benchmark.get_nvalue()
            samples.add(count)
            deviation = benchmark.stdev() if count > 1 else 0.0
            cells.append(f"{mean * 1e6:.3f} ± {deviation * 1e6:.3f}")
            if count > 1 and deviation / mean >= 0.10:
                noisy.append(benchmark.get_name())
        lines.append(f"| {scenario} | {' | '.join(cells)} |")
    lines.extend([
        "",
        f"Measured samples per benchmark: {', '.join(map(str, sorted(samples)))}.",
        "",
        "## Interpretation",
        "",
        "- Multi-error parsing returns three issues for Zodish, Pydantic and Marshmallow,",
        "  but only the first issue for msgspec. Those cells measure different work.",
        "- `error_string` formats an existing single-error exception. Libraries differ",
        "  in eager versus deferred formatting and message detail; treat it as diagnostic.",
        "- `schema_build` measures each adapter's factory, including Python wrapper creation.",
        "  Pydantic uses an existing TypedDict; Marshmallow creates a schema class and instance;",
        "  Zodish creates a schema graph. These are setup costs, not identical compiler work.",
        "- JSON cases include decoding and validation. Zodish uses `parse_json`,",
        "  Pydantic uses `validate_json`, msgspec uses `json.decode`, and Marshmallow",
        "  uses stdlib `json.loads`.",
        "- All timed inputs use the documented common subset. msgspec and Marshmallow",
        "  additionally accept tuples for list fields; the preflight records that difference.",
        "- Ordinary unions are left-to-right. Tagged unions are separate scenarios.",
        "  Only Zodish and Pydantic participate in these dictionary-output union cases.",
        "- Recursive schemas are prepared before timing, including Zodish's lazy target.",
        "",
    ])
    if noisy:
        lines.extend([
            "**High variability (standard deviation ≥ 10% of mean):**",
            "",
            *(f"- `{name}`" for name in noisy),
            "",
            "Repeat these measurements on an idle machine before interpreting small differences.",
            "",
        ])
    lines.extend([
        "The variability check above is a simple descriptive flag, not a significance test.",
        "Inspect the raw pyperf results for worker settings, warmups and stability warnings.",
        "",
        "## Reproducibility fingerprints",
        "",
    ])
    for key in ("benchmark_sha256", "zodish_python_sha256", "zodish_native_sha256"):
        lines.append(f"- {key}: `{metadata.get(key, 'unknown')}`")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    pyperf = importlib.import_module("pyperf")
    text = render(pyperf.BenchmarkSuite.load(str(args.results)))
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
