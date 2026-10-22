# Native dataclass codec: validated-class decode/encode move into Rust

Validated dataclass codecs (`z.codecs.dataclass` on `@z.dataclass` classes)
now decode and encode through two native node kinds, `dataclass_decode` and
`dataclass_encode`, instead of Python orchestration closures around per-field
native calls. The validating constructor keeps its Python assembly loop: a
measured switch to a native assembly call was reverted after it regressed
(see below). No public API changed.

These are single-machine paired measurements on an editable **debug** build,
not a new performance gate and not release-wheel numbers.

## What changed

- `dataclass_decode` / `dataclass_encode` native kinds evaluate a whole
  instance in one native pass: supplied-field validation, defaults and
  factories, annotation output guards, InitVar routing, `__post_init__`,
  `init=False` fields, wire names, and frozen/slots assembly.
- Sync evaluation lives in `rust/schema/evaluate/dataclass.rs`; async
  session frames live in `rust/session/containers/dataclass.rs` with shared
  default-resolution and assembly helpers, so async validated classes, async
  codec fields, and recursive classes use the same nodes.
- Plain (undecorated) dataclass codecs keep the previous closure path.
- The constructor path (`__init__`/`acreate`) is unchanged except hoisting
  two per-call dict rebuilds to decoration time in `acreate`.

## Snapshots and environment

- Before: working tree parent state (closure orchestration); after: working
  tree with the native nodes. Both measured against commit `33a2d14`'s tree
  with the same editable debug extension; "before" runs used the stashed
  Python builder so only orchestration differed, not the native evaluator.
- Host: Apple M4 Max, 48 GiB RAM, macOS 27.0.1 arm64. CPython 3.14.3 with
  the GIL enabled.
- Rust 1.99.0 (`b940084d7`); maturin editable install; PyO3 `=0.30.1`,
  `abi3-py314`. Editable debug profile (no `--release`, no LTO).
- Extension SHA-256 (debug build):
  `b920e0fca42d2e4b848a12b4bfb2b0b23935fdcc5aa71b68bb49e17c8cbaf0c1`.
- Sampling window: 2026-10-22 22:4x UTC. No CPU affinity, frequency control,
  or claim of an otherwise idle machine.

## Paired measurements

Two fixtures were measured, both paired on the same extension build.

Micro fixture (3-field validated class `Point(x: int, y: int, label: str =
"p")`; `timeit`, min of 5 × 200,000 operations):

| Operation | Before µs/op | After µs/op | Change |
| --- | ---: | ---: | ---: |
| validated construction | 1.93 | 1.98 | +2.6% (unchanged within noise) |
| codec decode | 1.13 | 0.48 | −58% |
| codec encode | 1.05 | 0.48 | −54% |

The constructor row includes the reverted `_native.assemble` switch, which
measured 2.04 µs/op (+3.5%, stable across repeats) because the pyfunction
boundary crossing costs more than the small Python `object.__setattr__` loop
saves on typical small classes; it was reverted.

Harness fixture (4-field validated class: `id: int`, `name: str` trim/min 1,
`created` ISO-datetime codec field, `tags: list[str]` default factory;
`bench_parse.py --filter dataclass`, median of 5 runs):

| Scenario | Before µs/op | After µs/op | Change |
| --- | ---: | ---: | ---: |
| `dataclass_construct` | 2.56 | 2.55 | unchanged |
| `dataclass_decode` | 2.48 | 1.21 | −51% |
| `dataclass_encode` | 2.48 | 1.43 | −42% |

Construction is dominated by `inspect.Signature.bind` and is included as a
guard against constructor regressions, not as an optimization target.

## Full harness run

`benchmarks/bench_parse.py` (median of 5 runs per scenario, after state,
debug editable build):

| Scenario | ops/s | µs/op |
| --- | ---: | ---: |
| `string_success` | 6,582,375 | 0.15 |
| `object_success` | 2,866,082 | 0.35 |
| `object_failure` | 884,910 | 1.13 |
| `object_failure_issues` | 418,860 | 2.39 |
| `array_1000` | 182,523 | 5.48 |
| `union_first` | 4,925,303 | 0.20 |
| `union_last` | 1,539,686 | 0.65 |
| `dataclass_construct` | 394,547 | 2.53 |
| `dataclass_decode` | 819,157 | 1.22 |
| `dataclass_encode` | 692,489 | 1.44 |
| `recursive_tree` | 102,884 | 9.72 |
| `refine_callback` | 6,382,981 | 0.16 |

`recursive_tree` exercises recursive validated classes only indirectly; the
dataclass rows above are the new coverage. Async decode/encode of validated
classes run through the session frames; `bench_async.py --smoke` passes but
was not timed for this record.

## Governance

Per `benchmarks/README.md`, these are exploratory single-machine stdlib
measurements on a debug build. They record direction and rough magnitude of
the orchestration move; they are not a significance test, not comparable to
the release-wheel pyperf records, and not a performance gate. A
publication-quality release-wheel paired run remains future work.
