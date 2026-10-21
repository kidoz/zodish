# Recorded benchmark runs

## 2026-10-21: Release async harness, macOS ARM64, CPython 3.14.8

- [Async summary and synchronous comparison](2026-10-21-macos-arm64-async-release.md)
- [Raw async samples](2026-10-21-macos-arm64-async-release.json)
- [Raw synchronous samples](2026-10-21-macos-arm64-sync-release.json)
- [Benchmark contract and installation instructions](../README.md)

Zodish was installed from a 0.4.0 release-candidate `abi3` wheel built from the
working tree at `f159880` plus the async changes. The async harness ran nine
repeated samples per scenario with memory reporting; the synchronous
`bench_parse.py` harness ran nine samples against the same isolated wheel and
Python 3.14.8 interpreter.

Median results included 29.7 µs/operation for a synchronous graph through
`parse_async`, 33.8 µs for an immediately completing async callback, 90.5 µs
when four callbacks each yield through `asyncio.sleep(0)`, 152.1 µs for eight
sequential callbacks, and 249.6 µs per 16-parse concurrent batch (about 64,000
individual parses/s). The summary reports sample standard deviation, min/max,
and retained/peak `tracemalloc` bytes; raw files retain every per-sample median.
These are local exploratory measurements, not performance thresholds. The
summary contains wheel, benchmark, installed Python, and installed native
hashes.

## 2026-10-20: Post-optimization rerun, macOS ARM64, CPython 3.14.8

- [Summary table](2026-10-20-macos-arm64-rerun.md)
- [Raw pyperf results](2026-10-20-macos-arm64-rerun.json)
- [Initial baseline](2026-10-20-macos-arm64.md)
- [Benchmark contract and installation instructions](../README.md)
- [Exact benchmark dependency versions](../requirements.lock)

This rerun uses the same interpreter patch version, pinned library versions,
scenario set, and runner options as the initial baseline. All 46 supported
library/scenario combinations passed the semantic preflight. Zodish was
installed from a release wheel built from the current working tree.

Command, executed from the repository root in a dedicated environment:

```sh
python benchmarks/bench_compare.py --build-profile release \
  --processes 3 --values 5 --warmups 2 --min-time 0.05 \
  -o benchmarks/results/2026-10-20-macos-arm64-rerun.json

python benchmarks/summarize.py benchmarks/results/2026-10-20-macos-arm64-rerun.json \
  --output benchmarks/results/2026-10-20-macos-arm64-rerun.md

python -m pyperf check benchmarks/results/2026-10-20-macos-arm64-rerun.json
```

Compared with the initial Zodish baseline, arrays improved 5.28x, recursive
trees 3.25x, multi-error object failures 3.89x, single-error failures 2.33x,
JSON objects 2.65x, and object success 1.47x. Union and discriminated-union
scenarios improved by 1.32x to 2.33x. The refinement callback improved from
0.356 µs to 0.166 µs. Constrained strings were within measurement noise of the
initial run. `error_string` measured 58 ns versus 24.0 ns; the absolute
difference is small, it remains faster than Pydantic and Marshmallow, and this
cached-formatting row is sensitive to interpreter call-path changes.

The JSON row is not directly comparable across the two runs: the initial
baseline used stdlib `json.loads` plus `parse` for Zodish, while this rerun uses
the new native `parse_json` API. The current mapping matches the other adapters'
integrated JSON APIs.

Within the rerun, Zodish is faster than Pydantic for constrained strings, the
refinement callback, both union branches, both discriminated-union branches,
error formatting, and schema construction, and faster than msgspec for the
single-error row. It is effectively tied on object success (1.03x), arrays
(1.01x), JSON with msgspec, and the last union branch. The largest remaining gaps
are recursive trees (1.69x Pydantic, 2.65x msgspec), JSON objects (1.29x
Pydantic), multi-error construction (1.22x Pydantic, 1.03x msgspec), and
single-error construction (1.19x Pydantic). No Zodish row exceeded the summary's
10% variability threshold.

pyperf reports insufficient sampling for many fine-grained stability checks, as
in the initial run. Use longer idle-machine runs for small differences. The
hashes below identify the measured code:

- benchmark_sha256: `cae9f3e2f60f46eef499d4f442ca4a9daf30f8093fc13e24267fd2d6699e1a2c`
- zodish_python_sha256: `b928260c0ef767eb150e1097bbc37778ea8ad1090b63f54e16f29a7ced2615ee`
- zodish_native_sha256: `54e0970f4bedaae309e3d8089a8106627811a510ca00f00338d22bf5f9782d72`

## 2026-10-20: Initial baseline, macOS ARM64, CPython 3.14.8

- [Summary table](2026-10-20-macos-arm64.md)
- [Raw pyperf results](2026-10-20-macos-arm64.json)
- [Benchmark contract and installation instructions](../README.md)
- [Exact benchmark dependency versions](../requirements.lock)

This is an exploratory run on the development machine, not an isolated
performance lab. All 46 supported library/scenario combinations ran successfully
with three measured worker processes, five values per worker, and two warmups.
pyperf also performs calibration. There are 15 measured samples per case.

Command, executed from the repository root in a dedicated environment:

```sh
python benchmarks/bench_compare.py --build-profile release \
  --processes 3 --values 5 --warmups 2 --min-time 0.05 \
  -o benchmarks/results/2026-10-20-macos-arm64.json

python benchmarks/summarize.py benchmarks/results/2026-10-20-macos-arm64.json \
  --output benchmarks/results/2026-10-20-macos-arm64.md

python -m pyperf check benchmarks/results/2026-10-20-macos-arm64.json
```

Zodish was installed from a wheel produced with
`maturin build --release --locked --interpreter <environment-python>`.
The checkout was being edited during the task, so the run deliberately retained
that installed wheel throughout. It is a local working-tree snapshot, not a
clean tagged release. Installed Python and native code hashes are included in
the summary and raw data. Every case has the same benchmark script fingerprint.
Rerun the suite to measure later changes.

All adapters passed the semantic preflight. Additional checks confirmed fresh
object dictionaries, nested tag lists, and array outputs for the tested success
fixtures in every library.

The release wheel passed the then-current 70-test unittest suite, quickstart,
and native-extension verification. Rust formatting and Clippy passed. The final
benchmark sources passed Ruff and strict mypy; the final repository-wide Ruff
and mypy checks also passed. A selected-library pyperf worker run verified
argument forwarding. The configured CI/platform matrix was not run locally.

pyperf reported insufficient sampling for a number of its fine-grained
stability checks. Pydantic's array and JSON scenarios also had standard
deviation at least 10% of their means. Repeat on an idle machine with longer
runs before interpreting small differences. The generated report's variability
flag is descriptive and does not replace pyperf's own checks.

At the time, the results identified arrays, recursive traversal, and error
construction as useful Zodish optimization targets. The constrained-string
scenario was faster than the three alternatives in this run. Comparisons of
multi-error handling, error string formatting, and schema setup require the
qualifications in the benchmark contract; they perform different amounts or
kinds of work.
