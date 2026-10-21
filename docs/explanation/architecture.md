# Architecture

Zodish is a Python API over a Rust validation core. This page explains the split,
what it buys, and what it costs.

## One evaluation boundary

Python schema builders construct native schema nodes. A call to `parse`,
`safe_parse`, or `validate` enters the Rust evaluator once, and Rust traverses
the schema, validates values, constructs collections, applies bounds, handles
optional/default behavior, evaluates unions, and aggregates issues. There is no
Python validation fallback: if the native extension is unavailable, the package
does not validate in pure Python.

The public result and error classes, and the static typing surface, remain Python
code.

`parse_json` adds a native JSON decoder for a supported JSON-compatible subset,
including primitive leaves, numeric constraints, literals/enums, nullable and
optional wrappers, and strict objects without extra keys. When the schema or
value falls outside that subset, the input is decoded with Python's `json` module
and validated through the same evaluator, so the fallback does not change
validation semantics.

Async validation extends the boundary with a native resumable session. Rust owns a continuation frame stack for containers, wrappers, unions,
intersections, pipes, and lazy schemas, and retains snapshots, paths, issues, and
the per-parse lazy cache. Synchronous subtrees without async descendants still
use the ordinary native evaluator and its fast paths. The Python adapter only
awaits a requested callback result and resumes the session; it never walks
schemas or validates values.

## What stays in Python

Some behavior must run with Python semantics:

- user callbacks from `refine`, `super_refine`, `transform`, `overwrite`,
  `preprocess`, `pipe`, and codec `decode`/`encode`;
- Python regular expressions;
- string case and whitespace operations such as `lower`, `upper`, and `trim`;
- `deepcopy` for defaults, preserved extra fields, and fallbacks.

Crossing the boundary for these operations preserves existing Python behavior at
the cost of a Python call. Common exact synchronous callback result types bypass
async inspection; other result types still receive full awaitable and
async-generator checks. The rest of a parse stays in Rust.

## Bidirectional codecs

A codec wraps an encoded schema, a decoded schema, and two Python callbacks.
Decoding parses the encoded schema, calls `decode`, and parses the decoded
schema. Encoding uses a mirrored reversible node tree: codec nodes use their
`encode` callback, containers traverse their children in reverse, and one-way
operations make the tree unavailable. Both directions remain inside the native
evaluator; only the codec callbacks cross into Python.

Because encoding is derived from the schema structure, it is available for
primitives, constraints, codecs, optional/nullable wrappers, and structural
containers. Transforms, refinements, coercions, defaults, catches, pipes,
preprocessors, and lazy schemas have no reverse operation, so `.encode()` raises
`TypeError` when they occur in the tree.

## Immutable schemas and ordered operations

Every fluent method returns a new schema. This makes schemas safe to share and
compose, and it makes the operation sequence explicit in the code.

Operations execute in declaration order. `.trim().min(1)` validates the trimmed
string, while `.min(1).trim()` checks the raw string length first. Object
structural methods edit the shape; refinements and transforms return a general
`Schema[T]`, so type-specific methods should come before them.

## Error aggregation

Validation collects sibling issues instead of stopping at the first failure.
Object key order and collection indices are included in issue paths. A union
failure keeps every branch's issues, which makes failed alternatives inspectable
rather than collapsing them into a single message.

This design favors complete diagnostics for forms and APIs. The trade-off is that
a parse with many failures does more work than a fail-fast parse. Failures hold a
GC-aware native issue bundle; Python `Issue` objects and formatted messages are
created only when read. Common constraint and literal failures also reuse native
error templates. Array and tuple indices remain native path parts until an issue
is materialized.

## Recursion and container snapshots

Inputs must be acyclic. The native evaluator raises `RecursionError` at 256 nested
schema-node evaluations, including wrappers, to bound Rust stack use. Each lazy
node resolves its factory once per parse and reuses the result for the rest of
that parse; later parses resolve it again. Python callbacks may additionally hit
Python's recursion limit.

Containers are shallow-snapshotted before traversal, so a callback can mutate the
original list or dictionary without invalidating the iteration.

## Performance

The native core is the reason for the Rust dependency. Typical benchmark results
(Apple M4 Max, Python 3.14.3, `parse` unless noted):

| Scenario | Operations/sec | Time per operation |
| --- | ---: | ---: |
| `z.string().min(3).max(32)` | 7,475,470 | 0.13 µs |
| Object parse | 2,642,532 | 0.38 µs |
| Object failure (`safe_parse`) | 977,485 | 1.02 µs |
| Object failure, issues inspected | 437,031 | 2.29 µs |
| 1,000-element integer array | 173,938 | 5.75 µs |
| Union, first branch | 4,802,304 | 0.21 µs |
| Union, last branch | 1,457,000 | 0.69 µs |
| Recursive tree (depth 4) | 110,799 | 9.03 µs |
| Refinement callback | 7,066,718 | 0.14 µs |

Numbers are medians over seven repetitions from `benchmarks/bench_parse.py`. The
array row uses the native fast path for primitive item validators and exact-list
iteration, the recursive row benefits from per-parse lazy resolution, object
parsing skips the unknown-key scan and input copy when every input key belongs
to the shape, and failures defer Python `Issue` materialization and message
formatting until they are read. Materializing `.issues` builds and caches the
`Issue` objects on first access. Results depend on the host, Python version, and
native build profile; the harness has no pass/fail thresholds.

## Packaging consequences

Python builders plus a native core require maturin and PyO3 at build time. Wheels
use the GIL-enabled CPython 3.14+ stable ABI and must match the operating system
and architecture. Installing a compatible prebuilt wheel does not require Rust;
building from source does. Free-threaded wheels are not part of the published
compatibility matrix.

An editable install picks up Python edits but does not rebuild Rust. After
changing `rust/lib.rs`, reinstall the package before testing.

## Related

- [Contribute to Zodish](../how-to/contribute.md)
- [Build distributions](../how-to/build-distributions.md)
- [Type semantics](type-semantics.md)
- [API reference](../reference/api.md)
