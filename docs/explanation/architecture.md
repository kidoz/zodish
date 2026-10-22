# Architecture

Zodish is a Python API over a Rust validation core. This page explains the split,
what it buys, and what it costs.

## Rust source layout

`rust/lib.rs` declares the modules, shares the native depth limit, and registers
the Python module. `rust/session.rs` owns `AsyncSession`, its frame/state types,
token counter, Python methods, and GC lifecycle. Its fields stay private;
suspended frames retain snapshots, callbacks, issues, and tracebacks.
`rust/session/dispatch.rs` contains the execution loop, outcome delivery, and
frame routing. Only its `run` and `advance` entry points are visible to the
session parent. `rust/session/evaluate.rs` owns async-aware node evaluation,
continuation setup, lazy resolution, and routing of synchronous subtrees to the
ordinary native evaluator. Its `eval_node` method has session visibility;
frame/state storage and lifecycle remain in `session.rs`.
`rust/session/wrappers.rs` contains catch, refinement, post-child, string-bool,
and pipe continuations. Its six methods are visible
within the session module; frame storage and lifecycle remain in `session.rs`.
`rust/session/containers.rs` declares three child modules and shares a private
path helper. `containers/object.rs` owns object snapshots, field/extras traversal,
primitive field fast paths, and issue aggregation. `containers/sequence.rs` owns
array/set/tuple snapshots, item traversal, and result construction;
`containers/record.rs` owns record snapshots and key/value traversal. Each child
contains its three start/advance/resume methods, restricted to session visibility
with `pub(in crate::session)`. Frame/state definitions remain in `session.rs`.
`rust/session/composition.rs` contains union/intersection
continuations, outcome storage/restoration, and intersection result combination.
Its six dispatch entry points have session visibility; outcome helpers stay
private. Frame and stored-outcome types remain in `session.rs`.
`rust/session/callbacks.rs` contains callback resumption, awaitability checking,
pending-request creation, and callback-result interpretation. Its entry points
have session visibility; evaluation and wrappers import the shared awaitability
checker directly. Pending/frame storage, the token counter, Python lifecycle methods,
and GC hooks remain in `session.rs`. Wrapper continuations use the same
awaitability checker and shared post-child checks. `rust/session/gc.rs` visits
references held by suspended frames, including issues, stored exceptions, and
tracebacks. Its visitor is visible only within the session module; Python GC
hooks delegate
frame traversal to it. Terminal-state cleanup stays with the session lifecycle.
Paths, issue/error types, and the GC-aware native issue bundle live in
`rust/errors.rs`.
`rust/runtime.rs` holds the per-parse `Context`, callback-error normalization,
and lazy caches: local storage for sync parsing and session-owned shared storage
for async parsing. Session GC traversal and cleanup in `rust/session.rs` visit
and release the same cached references. `rust/stack.rs` checks the thread's
remaining stack before each native evaluation so deep or re-entrant recursion
raises `RecursionError` instead of overflowing the stack.
`rust/values.rs` shares Python value predicates, numeric multiple checks,
coercion, error-value formatting, JSON-value compatibility checks, dictionary
merging, and copying across validation paths. `rust/schema/evaluate.rs` owns
synchronous dispatch, primitive validation, composition, and depth checks.
`rust/schema/evaluate/containers.rs` owns synchronous sequence and mapping
evaluation, container snapshots, result construction, issue aggregation, and
primitive fast paths. Its two evaluation methods are visible only within the
evaluator module; its snapshot buffer and list fast-path helper stay private.
Async sessions reuse the synchronous evaluator for synchronous subtrees and
share the primitive-kind classification in `evaluate.rs`.
`rust/schema.rs` defines `NativeSchema`, its unknown-key policy, constructor,
capability flags, Python-facing methods, and GC integration. Its parse entry
points call sync validation in `schema/evaluate.rs` and JSON decoding and fast
evaluation in `schema/json.rs`. JSON helpers are private to the schema module.
Cached error templates are private to the schema module and its children.
Operation kinds are exposed from `rust/schema/kind.rs`, where operation names
and constructor arity are checked. `rust/schema/checks.rs` contains shared
post-child operations and constraint checks, plus the error templates built
during node construction. Sync evaluation and async continuations call the same
implementation. The evaluators share these definitions;
validation behavior and Python-visible class names are unchanged by this split.

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
schema-node evaluations, including wrappers, to bound Rust stack use. It also
raises `RecursionError` when the remaining thread stack is too small, so a
callback that re-enters `parse` on a small-stack thread fails cleanly instead of
exhausting the native stack. Each lazy node resolves its factory once per parse
and reuses the result for the rest of that parse; later parses resolve it again.
Python callbacks may additionally hit Python's recursion limit.

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

A [release comparison of the Rust module split](../../benchmarks/results/2026-10-21-macos-arm64-rust-decomposition.md)
records 12 paired rounds across 22 scenarios, with unchanged Python code and
benchmark fixtures. Its raw samples, variation, source review, and build hashes
are available with the report.

## Packaging consequences

Python builders plus a native core require maturin and PyO3 at build time. Wheels
use the GIL-enabled CPython 3.14+ stable ABI and must match the operating system
and architecture. Version-specific free-threaded `cp314t`/`cp315t` wheels are
published for Linux x86_64/aarch64 glibc, macOS x86_64/arm64, and Windows x64.
Installing a compatible prebuilt wheel does not require Rust; building from
source does. Free-threaded musllinux wheels are not published.

An editable install picks up Python edits but does not rebuild Rust. After
changing any source under `rust/`, reinstall the package before testing.

## Related

- [Contribute to Zodish](../how-to/contribute.md)
- [Build distributions](../how-to/build-distributions.md)
- [Type semantics](type-semantics.md)
- [API reference](../reference/api.md)
