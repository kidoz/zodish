# Rust module decomposition: review and release comparison

The final review found no unintended source-body or API changes. Across 22
scenarios, median paired latency changes ranged from **2.13% faster to 1.42%
slower**. No large slowdown was observed in this run. These measurements do not
establish strict equivalence or performance on other hosts.

The [raw samples, metadata, and replay drivers](2026-10-21-macos-arm64-rust-decomposition.json)
retain every observation. No samples were removed or selected for rerunning.

## Snapshots and environment

- Before: `c4cf19027a9cac02cb9e962e7ad1c8adcf5465aa`, the parent of the first
  extraction commit `85045f1`.
- After: `1dd1cd1563e317b2912d954499a42c7b47c41bc3`, the completed source split.
- Both snapshots are Zodish 0.5.1. Python code, tests, benchmark scripts,
  `Cargo.toml`, `Cargo.lock`, and `pyproject.toml` are byte-identical between them.
- Host: Apple M4 Max, 16 physical/logical CPUs, 48 GiB RAM,
  macOS 27.0.1 arm64. CPython 3.14.3 with the GIL enabled.
- Separate clean release builds and virtual environments for each snapshot;
  installed wheels imported from site-packages, outside both source trees.
- Rust 1.99.0 (`b940084d7`), LLVM 23.1.1; maturin 1.15.0; PyO3 `=0.30.1`,
  `abi3-py314`; `--release --locked`, thin LTO, one codegen unit,
  `MACOSX_DEPLOYMENT_TARGET=11.0`.

Native-extension SHA-256:

| Snapshot | SHA-256 |
| --- | --- |
| before | `be55102472e43cf43dff469d1bde716efe630c600ee970928980cfaf82840ea4` |
| after | `5bfb0e680daa12e2598f68e3e03f3a3bc3a3cecc2067d0e347cc32ae865454d0` |

## Sampling method

The unchanged `bench_parse.py` and `bench_async.py` measurement functions ran
nine synchronous and eight async scenarios. Five additional fixtures came from
`bench_compare.py`: JSON bytes, first/last discriminated-union branches,
formatting an existing error, and schema construction. Their existing checks
ran outside timing, and their `Case.call` functions used the synchronous
measurement loop. This is a stdlib measurement run, not a pyperf result file.

Calibration estimated iterations for a 200 ms sample independently in both
versions; the larger count became the shared count for each scenario. Twelve
paired rounds used fresh worker processes. Version order alternated every
round, while both members of a pair used the same shuffled scenario order.
Existing harness warmups ran before each measurement; async event loops were
created outside timed iterations. `PYTHONHASHSEED=0` was fixed in every worker.
There was no CPU affinity, frequency control, or claim of an otherwise idle
machine. Other builds and tests ran outside the timing window.

There are 528 retained samples (22 scenarios × 12 rounds × 2 versions), lasting
189–334 ms each. The sample window was 18:23:20–18:25:11 UTC on 2026-10-21.

Below, latency is the median in microseconds, with sample standard deviation
in parentheses. Paired change is `100 × (median(after_i / before_i) - 1)`;
it can differ from the ratio of the two displayed marginal medians. Negative
means faster. These descriptive values are not a significance test or a
project performance gate.

## Results

| Scenario | Before µs (SD) | After µs (SD) | Paired change |
| --- | ---: | ---: | ---: |
| `async/concurrent_parses` | 219.1126 (3.2822) | 221.2877 (4.5623) | +0.42% |
| `async/immediate_coroutine` | 31.4789 (0.4000) | 31.7226 (0.6430) | +0.37% |
| `async/nested_failures` | 33.1064 (0.8050) | 33.2830 (0.5624) | +0.35% |
| `async/nested_failures_issues` | 34.9808 (0.7141) | 34.9615 (0.1390) | -0.64% |
| `async/sequential_callbacks` | 142.2677 (2.5179) | 141.9292 (2.4792) | -0.32% |
| `async/sleep_zero` | 85.7962 (1.4253) | 86.1852 (2.8296) | +0.16% |
| `async/sync_baseline` | 0.3580 (0.0048) | 0.3601 (0.0062) | +0.33% |
| `async/sync_graph` | 28.6551 (0.4208) | 28.5794 (0.6670) | +0.30% |
| `extra/discriminated_first` | 0.2172 (0.0032) | 0.2143 (0.0023) | -1.44% |
| `extra/discriminated_last` | 0.2609 (0.0070) | 0.2606 (0.0043) | -0.35% |
| `extra/error_string` | 0.0437 (0.0035) | 0.0432 (0.0007) | -2.13% |
| `extra/json_object` | 0.5651 (0.0098) | 0.5661 (0.0167) | -0.13% |
| `extra/schema_build` | 8.5815 (0.0876) | 8.5920 (0.1119) | -0.04% |
| `sync/array_1000` | 5.5618 (0.0984) | 5.5490 (0.0841) | -0.51% |
| `sync/object_failure` | 0.9566 (0.0149) | 0.9632 (0.0168) | +0.46% |
| `sync/object_failure_issues` | 2.2132 (0.0325) | 2.2348 (0.0294) | +1.42% |
| `sync/object_success` | 0.3378 (0.0607) | 0.3361 (0.0035) | -0.65% |
| `sync/recursive_tree` | 8.7594 (0.1222) | 8.6342 (0.1617) | -0.99% |
| `sync/refine_callback` | 0.1409 (0.0024) | 0.1410 (0.0012) | +0.25% |
| `sync/string_success` | 0.1228 (0.0016) | 0.1218 (0.0010) | -0.43% |
| `sync/union_first` | 0.1891 (0.0019) | 0.1901 (0.0046) | +1.36% |
| `sync/union_last` | 0.6275 (0.0196) | 0.6349 (0.0092) | +1.35% |

Most per-version coefficients of variation were about 1–3%. The baseline
`sync/object_success` series had 17.1% variation, including one slow sample;
baseline `extra/error_string` had 7.9%. All are retained. The small apparent
improvements should not be interpreted as optimizations. The largest median
paired slowdown was 1.42% for `sync/object_failure_issues`; it is reported
without imposing a new threshold or claiming zero regression.

`async/concurrent_parses` measures a batch of 16 parses. Async rows include
Python event-loop transport. `async/sync_baseline` calls synchronous `parse`,
while `async/sync_graph` runs a synchronous graph through `parse_async`.
`extra/error_string` formats an already-created error; `extra/schema_build`
measures setup. Their numbers describe those particular fixtures.

## Refactor review and validation

The normalized Rust lexical comparison matched 118 functions/methods with
their implementation owners, 35 type definitions (including trait bodies),
five constants/aliases, and all 23 attributes. Normalization ignored comments,
whitespace, expected `pub(crate)`/`pub(super)` changes, and trailing commas.
No body changes were found. Module imports and visibility were reviewed
against their callers.

The review also verified:

- `lib.rs` has 24 lines for declarations, the shared depth limit, and unchanged
  registration. One Python method block remains with each native class.
- Schema construction/capability flags remain with `NativeSchema`; shared
  post-child checks are called by both execution paths. Async synchronous
  subtrees still use the synchronous evaluator.
- Session state, token counter, frame/pending/outcome storage, Python lifecycle
  methods, and cleanup remain together. Frame GC visitation moved intact and
  does not attach to Python or execute callbacks.
- Native class module names, qualified names, signatures, and weak-reference
  offsets match across installed wheels. Python stubs and public builders did
  not change.
- All 15 Rust sources are explicitly required by `scripts/verify_dist.py`.
  Architecture, contribution instructions, and canonical project skills point
  to the implemented modules.

Both isolated wheels passed all 132 tests with warnings as errors on CPython
3.14.3 and 3.15.0, plus native verification and quickstart on 3.14. Sync/async
benchmark smokes passed for both snapshots. Current Rust format/clippy, Ruff,
strict mypy, and comparison conformance checks passed. The candidate wheel and
sdist passed content verification; a clean wheel rebuilt from that sdist
passed all 132 tests, native verification, and quickstart on 3.14.

No refactor fixes were required. Linux, Windows, free-threaded Python, other
architectures, and publication-quality host-controlled performance runs were
not exercised here. The source split and its local review are complete;
normal cross-platform CI remains the next release check.

## Reproduction

Create a temporary run directory containing `before/source` and `after/source`
from `git archive` at the revisions above. Build each with the recorded command
and separate `CARGO_TARGET_DIR` directories, then install each wheel without
dependencies into `before/venv` and `after/venv`, using the same CPython 3.14
interpreter. Keep the original benchmark scripts in `after/source/benchmarks`.

Extract the replay drivers from the raw JSON into that temporary directory:

```python
import json
from pathlib import Path

raw = json.loads(Path("benchmarks/results/2026-10-21-macos-arm64-rust-decomposition.json").read_text())
run_dir = Path("/path/to/temporary/run")
(run_dir / "benchmark-worker.py").write_text(raw["worker_source"])
(run_dir / "run-benchmarks.py").write_text(raw["runner_source"])
```

Run `python run-benchmarks.py` from that directory. The runner recalibrates,
checks matching class/schema metadata, alternates version order, and writes
new raw samples. Installed package paths in the published JSON use
`<run-root>` instead of machine-specific temporary paths. Compiler, platform,
wheel, schema, harness, and Cargo hashes are retained for comparison.
