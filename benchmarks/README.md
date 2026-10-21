# Comparing validation libraries

`bench_parse.py` is a dependency-free synchronous Zodish microbenchmark.
`bench_async.py` measures the asynchronous path with the same style: synchronous
graphs through `parse_async`, immediately completing callbacks,
`asyncio.sleep(0)` suspension, many sequential callbacks, nested failures, and
concurrent parses of one schema. It creates event loops outside timed
iterations, reports median/sample standard deviation/min/max, and can report
retained and peak `tracemalloc` values.
`bench_compare.py` uses pyperf to compare public validation APIs against
Pydantic, msgspec, and Marshmallow. It measures Python dictionaries as output,
not model or Struct construction.

## Installation and execution

Use a dedicated virtual environment with GIL-enabled CPython 3.14+ and the Rust
toolchain. From the repository root:

```sh
python3.14 -m venv /tmp/zodish-bench
source /tmp/zodish-bench/bin/activate
python -m pip install --group benchmark 'maturin>=1.15,<2'
maturin build --release --locked --interpreter python --out /tmp/zodish-bench/wheels
python -m pip install --force-reinstall /tmp/zodish-bench/wheels/*.whl
python examples/verify_native.py
python benchmarks/bench_parse.py --smoke
python benchmarks/bench_async.py --smoke
python benchmarks/bench_compare.py --check-only
python benchmarks/bench_compare.py --build-profile release -o comparison.json
python benchmarks/summarize.py comparison.json --output comparison.md
```

The shell activation and wheel glob above are for POSIX shells. On Windows,
use a suitable temporary environment path, activate its `Scripts` directory,
and pass the wheel's explicit filename.

Top-level benchmark versions are pinned in the `benchmark` dependency group.
For the exact transitive versions used by the recorded local run, install
`benchmarks/requirements.lock` with `python -m pip install -r` instead of
`--group benchmark`. The lock targets Python 3.14; verify it before using a newer
interpreter. Runtime dependencies of Zodish remain unchanged.

`--build-profile` is descriptive metadata, not build detection. Do not label a
debug/editable build as release. The results record package versions, Python,
platform, GIL state, and hashes of the installed native extension, installed
Python schema implementation, and benchmark script. pyperf also records worker
and timing metadata.

Examples:

```sh
# Select a library or scenario family.
python benchmarks/bench_compare.py --library pydantic --scenario union

# Short exploratory run, not a publication-quality measurement.
python benchmarks/bench_compare.py --build-profile release \
  --processes 3 --values 5 --warmups 2 --min-time 0.05 -o comparison.json

# Longer run on an idle machine.
python benchmarks/bench_compare.py --build-profile release --rigorous -o rigorous.json

python -m pyperf stats comparison.json
python -m pyperf check comparison.json
python -m pyperf compare_to before.json after.json --table
```

pyperf refuses to overwrite an existing output file; use a new filename for each
run. Compare matching benchmark names across revisions using `compare_to`.
Use `summarize.py` to compare libraries within one result file.

## Scenarios and fairness

| Scenario | Zodish | Pydantic | msgspec | Marshmallow |
| --- | --- | --- | --- | --- |
| Constrained string, object, 1,000 integers | Yes | Yes | Yes | Yes |
| Recursive tree (31 nodes) | Yes | Yes | Yes | Yes |
| Single/multiple invalid fields | Yes | Yes | Yes, first error only | Yes |
| String formatting of an existing error | Yes | Yes | Yes | Yes |
| Ordinary union, first/last branch | Yes | Yes | Not included | Not included |
| Discriminated union, first/last tag | Yes | Yes | Not included | Not included |
| Positive-integer callback | Yes | Yes | Not included | Yes |
| User schema setup | Yes | Yes | Not included | Yes |
| JSON bytes to validated dictionary | Yes | Yes | Yes | Yes |

Every adapter checks each field's constraints, required fields, booleans and
strings supplied as integers, unknown-key stripping, nested invalid data, and
the supported callback/union behavior before timing. Timed successful scenarios
also verify output equality, root output type, and unchanged input. Schemas and
payloads are prepared outside parsing measurements.

- Pydantic uses `TypeAdapter` over `TypedDict`, with `strict=True`.
- msgspec uses `convert(..., strict=True)` over `TypedDict`.
- Marshmallow uses `Schema.load`, strict integer fields, required fields,
  explicit validators, and `EXCLUDE` for unknown keys.
- Strict flags do not make the libraries semantically identical. msgspec and
  Marshmallow accept tuples for list fields; Pydantic and Zodish reject them.
  Timed payloads use actual lists. Other Python subclass/conversion behaviors
  outside these fixtures are not claimed to be equivalent.
- All failure cases use the same catching wrapper around the public raising
  parser. They do not add Zodish's `Success`/`Failure` allocation to just one
  participant. Paths and issue counts are checked outside the timed operation.
- Multi-error fixtures produce three issues in Zodish, Pydantic and Marshmallow,
  and one in msgspec. Do not present that row as identical diagnostic work.
- Error formatting is timed separately on an already-created exception.
  Formatting detail and eager/deferred work differ between libraries.
- Ordinary Pydantic unions explicitly use `left_to_right`. Tagged unions are
  separate. msgspec's tagged Struct unions would change the output contract;
  Marshmallow has no corresponding built-in union field, so both are omitted.
- msgspec has no equivalent built-in integer callback or explicit schema
  compilation API in this comparison; those cells are omitted.
- `schema_build` includes each adapter's factory. Pydantic uses an existing
  TypedDict, Marshmallow creates a class and instance, and Zodish builds its
  schema graph. These represent each adapter's setup cost.
- The Zodish recursive target is built once and returned by `lazy`. This differs
  from `bench_parse.py`, whose factory builds a new object schema on each visit.
- JSON measurements include parsing bytes. Zodish uses `parse_json`, Pydantic
  uses `validate_json`, and msgspec uses `json.decode`; Marshmallow uses
  `json.loads`. Zodish's fast path covers this fixture; other schemas fall back
  to standard decoding plus normal validation.
- Adapter closures and public Python API overhead are part of the measurements.
  These are not isolated native-engine measurements.

The summary reports mean and sample standard deviation, with a simple flag for
standard deviation at least 10% of the mean. That flag is not a significance
test or a replacement for pyperf's stability checks. Avoid extrapolating from a
single machine or interpreting differences smaller than measurement noise.

## Sources and version selection

Versions were researched on 2026-10-20:

- [Pydantic 2.14.1](https://pypi.org/project/pydantic/2.14.1/) and
  [adapter reuse guidance](https://pydantic.dev/docs/validation/latest/concepts/performance/).
- [msgspec 0.22.0](https://pypi.org/project/msgspec/0.22.0/).
- [Marshmallow 4.3.1](https://pypi.org/project/marshmallow/4.3.1/).
- [pyperf 2.10.0](https://pypi.org/project/pyperf/2.10.0/) and
  [runner options](https://pyperf.readthedocs.io/en/latest/runner.html).

CI runs conformance, async/sync microbenchmark smoke tests, and a tiny worker
smoke test, without performance thresholds. The timing comparison is intended
for deliberate local runs.

Async measurements include event-loop transport in each operation. Use
`--memory` for a one-operation retained/peak snapshot, and record the build
profile and commit alongside published results. A successful correctness run
does not establish a performance improvement.

## Recorded local results

The latest [post-optimization rerun](results/2026-10-20-macos-arm64-rerun.md)
and its [raw pyperf data](results/2026-10-20-macos-arm64-rerun.json) use the same
interpreter and pinned dependency versions as the
[initial baseline](results/2026-10-20-macos-arm64.md). See the
[run notes](results/README.md) for commands, hashes, and the comparison. These
exploratory runs use installed release snapshots; their hashes identify the
measured code even if the checkout changes.

The [2026-10-21 async release run](results/2026-10-21-macos-arm64-async-release.md)
records the isolated release-wheel async harness alongside a synchronous
comparison, including per-sample variability, memory snapshots, and
reproducibility hashes.
