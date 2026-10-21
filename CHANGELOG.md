# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.0] - 2026-10-21

### Added

- Dataclass codecs accept `Annotated[T, schema]` field definitions, including
  codecs and async schemas, and resolve PEP 695 type aliases inside supported
  annotations. Generic aliases support ordinary type parameters, nested
  substitutions, and defaults referring to earlier parameters. Explicit
  `fields=` entries take precedence.
- `z.typed_object(TypedDictClass, shape)` produces a statically typed
  `TypedDict` result while preserving ordinary object policies and runtime
  behavior.
- `.brand(NewType)` applies a typing-only branded output type without changing
  runtime validation.

### Changed

- Move dataclass annotation resolution and schema inference into a private
  module, preserving validation, typing, and codec behavior.
- Dataclass inference now enforces Zodish schema metadata in `Annotated`, which
  was previously ignored. Multiple schema entries raise `TypeError`; unrelated
  annotation metadata remains ignored.

### Fixed

- Typed objects now reject required `TypedDict` fields that resolve to
  `MISSING`, including optional schemas and transforms. Defaults run before
  the native output check, optional keys retain their schema behavior, and
  sync/async validation reports aggregated `required` issues at field paths.
- Typed object key qualifiers are resolved with postponed annotations, and
  JSON Schema export preserves field metadata and codecs while marking
  required output keys and allowing defaulted input keys to be omitted.

## [0.6.0] - 2026-10-21

### Added

- `z.set(item)` validates Python sets with native element traversal and async
  callback composition. Sets are not representable in JSON Schema or reverse
  encoding.
- `z.map(key_schema, value_schema)` validates dictionary keys and values using
  the native record evaluator, with validated key and value issue paths.
- `z.string()` format checks for ULID, CUID, CUID2, Nano ID, JWT, E.164,
  base64, and base64url.

### Changed

- Split the Rust core into focused modules for errors, schema definitions,
  values, synchronous evaluation, JSON handling, runtime state, and async
  sessions. Native behavior, public APIs, and Python-visible classes are
  unchanged. A paired 22-scenario release comparison recorded median changes
  from 2.13% faster to 1.42% slower.

## [0.5.1] - 2026-10-21

### Fixed

- Intersection parsing bounds recursive dictionary merging at the native depth
  limit, raising `RecursionError` instead of overflowing the Rust stack for
  deeply nested or cyclic dictionaries.
- Synchronous codec combinators (`optional`, `nonoptional`, `nullable`,
  `record`, array, union, and tuple codecs) and dataclass codecs now validate
  encoded and decoded values once, so input/output transforms and refinement
  callbacks run exactly once per value as they do for direct and async codecs.
- Integer `multiple_of` checks with integral float divisors at or above
  `2**63` now use exact arithmetic instead of saturating the divisor to
  `i64::MAX`.

## [0.5.0] - 2026-10-21

### Changed

- Async validation is stable as of 0.5.0. Composable async callbacks, JSON
  parsing, codecs, dataclass codecs, and reverse encoding are no longer labeled
  experimental. Synchronous validation remains the default and unchanged.
- Lazy factories remain synchronous as a permanent API boundary. A lazy factory
  may return a schema containing async operations, so recursive async schemas
  are supported without async schema construction.
- Official wheels remain GIL-enabled CPython 3.14+ `abi3` artifacts.
  Free-threaded builds passed local macOS arm64 CPython 3.14t and 3.15t test
  runs, but free-threaded wheels are not published in 0.5.0.

### Fixed

- Async parsing now reports `required` for missing input at the root of object,
  array, tuple, record, intersection, discriminated-union, and lazy container
  schemas, matching synchronous parsing. Nested missing container fields use
  the same rule.

## [0.4.0] - 2026-10-21

### Added

- Experimental async validation through `parse_async`, `safe_parse_async`,
  `refine_async`, `super_refine_async`, and `transform_async`. A native
  resumable frame stack owns validation, container snapshots, paths, issues,
  lazy state, and callback continuations; Python only awaits the callback
  result and resumes the session. Async callbacks compose with objects, arrays,
  records, tuples, unions, intersections, discriminated unions, pipes, wrapper
  ordering, defaults, catches, synchronous lazy factories, and synchronous
  callbacks between suspensions. Sessions are thread-affine, isolate
  concurrent parses, participate in GC, and close rejected or explicitly
  abandoned coroutines on cleanup.
- `parse_json_async` and `safe_parse_json_async` parse JSON `str`, `bytes`, or
  `bytearray` input and await nested async callbacks. Schemas covered by the
  native JSON subset keep its fast path; other schemas use standard decoding
  followed by asynchronous validation. Invalid JSON preserves the existing
  `invalid_format` issue and position.
- `validate_async` returns whether asynchronous validation succeeds while
  propagating programming errors, matching `validate`.
- Additional async callback families: `z.preprocess_async`,
  `.default_factory_async()`, `.catch_async()`, and `.overwrite_async()`.
  Preprocessing and default factories await their result before child
  validation; async catch fallbacks run only after native validation failures;
  overwrite preserves the static output type. Async lazy factories remain
  unsupported.
- `z.codec_async`, `.decode_async()`/`.safe_decode_async()`, and
  `.encode_async()`/`.safe_encode_async()`. Async codecs preserve encoded and
  decoded schemas, wrappers, records, arrays, unions, and tuples; async reverse
  graphs use the same native continuation session. Sync codecs and reversible
  schemas also work through the async encoding entry points. Dataclass codecs
  support async field codecs through the same async methods while preserving
  field issue prefixes and dataclass defaults.
- `benchmarks/bench_async.py` measures synchronous schemas through
  `parse_async`, immediately completing callbacks, `asyncio.sleep(0)`
  suspension, sequential callbacks, nested failures, and concurrent parses of
  one schema, with optional retained/peak memory reporting.

## [0.3.0] - 2026-10-21

### Changed

- Arrays whose item schema is a primitive validator (string, integer, number,
  boolean, none, or unknown) use a native fast path, and exact lists snapshot
  through direct list iteration. A 1,000-integer parse improved from roughly
  31 µs to 5.8 µs in the local release benchmark.
- Lazy schema factories resolve once per lazy node per parse instead of once
  per visit, using a compact per-parse cache. Factories are still invoked again
  on subsequent parses.
- Object traversal pre-reads declared field values without copying exact-shape
  inputs, caches the unknown-key policy, and validates primitive fields without
  constructing a path. Recursive trees improved from 18.8 µs to 9.0 µs in the
  local release benchmark.
- Failure construction is cheaper: `ValidationError` formats its combined
  message lazily, and native failures retain a GC-aware native issue bundle
  until `.issues` is read. The first access materializes and caches the public
  `Issue` objects without copying params or running dataclass initialization.
  Checks that only need success or failure, including `validate()`, avoid both
  raw and Python issue conversion. Object failure improved from 2.77 µs to
  1.05 µs in the local release benchmark; inspecting the materialized issues
  takes 2.29 µs.
- Constraint, literal, enum, and primitive type failures reuse native error
  templates instead of rebuilding parameter dictionaries and messages. The
  last-union branch improved from 1.17 µs to 0.69 µs locally.
- Callback async guards fast-path common exact synchronous result types before
  falling back to `inspect.isawaitable` and `inspect.isasyncgen`. Refinement
  callbacks improved from 0.32 µs to 0.14 µs in the local benchmark.
- Small array and tuple snapshots use inline storage for up to four items, and
  lazy nodes check a last-resolved entry before scanning the per-parse cache.
  Recursive trees improved from 9.2 µs to 9.0 µs locally.
- Object parsing skips the unknown-key scan and temporary known-key map when
  the input contains no keys outside the shape, including when optional fields
  are absent. Object success improved from 0.53 µs to 0.38 µs in the local
  release benchmark.

### Added

- `Schema.parse_json()`, `.safe_parse_json()`, and `.validate_json()` accept
  `str`, `bytes`, or `bytearray` JSON input. A native fast path validates
  JSON-compatible objects and arrays with primitive leaves, numeric bounds and
  multiples, literals and enums, nullable/optional wrappers, optional object
  fields, and string/array length constraints. Other schemas fall back to
  standard JSON decoding and normal validation. Invalid JSON reports
  `invalid_format` with a `position`.
- CPython 3.15 support: the `abi3-py314` wheel runs on 3.15, CI covers 3.14 and
  3.15, and the package metadata declares both versions.
- Bidirectional codecs through `z.codec(input_schema, output_schema, *,
  decode=..., encode=...)`, with `.decode()`/`.encode()`,
  `.safe_decode()`/`.safe_encode()`, reverse validation through containers, and
  JSON Schema input/output selection. One-way operations such as transforms,
  refinements, coercions, defaults, catches, pipes, preprocessors, and lazy
  schemas make encoding unavailable.
- Built-in codecs: `z.codecs.iso_date()`, `iso_time()`, `iso_datetime()`,
  `decimal()`, `json_text()`, `path()`, and `bytes_base64()` with standard or
  URL-safe base64.
- Dataclass codecs through `z.codecs.dataclass(Cls, fields=None)`, mapping
  encoded dictionaries to dataclass instances. Field schemas are inferred from
  common type annotations, including nested dataclasses, lists, records, fixed
  tuples, optional fields, and unions, and may be overridden per field.
  Recursive dataclass cycles are rejected. Dataclass defaults may be omitted
  from input, and input/output-mode JSON Schema describe the encoded and decoded
  object shapes respectively.
- Public codec combinators: `z.codecs.array()`, `z.codecs.union()`,
  `z.codecs.tuple()`, and codec `.record()`, `.optional()`, `.nullable()`,
  `.nonoptional()`, `.nullish()` methods. Compound codecs now retain
  output-side JSON Schema across nested dataclasses and containers.
- Exclusive numeric comparisons (`.gt()`, `.gte()`, `.lt()`, `.lte()`), exact
  integer and tolerant float `.multiple_of()` checks, string
  `.starts_with()`/`.ends_with()`/`.includes()` predicates, and `.ipv6()`.
- JSON Schema export for exclusive bounds, `multipleOf`, string predicate
  patterns, and IPv6 formats, with multiple checks emitted through `allOf`.
- Fixed-length tuple typing: `z.tuple()` infers precise output tuples for up to
  four items; larger tuples and tuples with a rest schema return
  `tuple[Any, ...]`.
- Parse benchmarks covering successful and failing parses, a 1,000-element
  array, union branch position, recursive schemas, and a refinement callback.
- Packaging CI that builds the wheel and sdist on Ubuntu, macOS, and Windows,
  verifies artifact contents and version agreement, and installs and smoke-tests
  each artifact.

## [0.2.0] - 2026-10-20

### Added

- Immutable, composable schemas for CPython 3.14+, with a Rust validation core
  and a typed Python API.
- Primitive schemas: `z.string()`, `z.number()`, `z.integer()`, `z.boolean()`,
  `z.none()`, `z.unknown()`, and `z.instance_of()`.
- String constraints and transforms (`.min()`, `.max()`, `.regex()`, `.email()`,
  `.trim()`, `.lower()`, `.upper()`) and numeric constraints (`.min()`, `.max()`,
  `.positive()`, `.nonnegative()`, `.int()`).
- Object schemas with `.strict()`, `.strip()`, `.passthrough()`, `.extend()`,
  `.pick()`, `.omit()`, and `.partial()`, plus array, tuple, and record schemas.
- Choice schemas: `z.literal()`, `z.enum()`, `z.union()`, and `schema.or_()`.
- Composition through `.optional()`, `.nullable()`, `.nullish()`, `.default()`,
  `.default_factory()`, `.refine()`, `.transform()`, `.pipe()`,
  `z.preprocess()`, and `z.lazy()` for recursive schemas.
- `parse()` and `safe_parse()` with native-only evaluation, aggregated issues,
  nested error paths, union branch details, and JSON-serializable
  `ValidationError.to_dict()` output.
- Arbitrary-precision integer comparisons for bounds and literals, with
  type-exact literals and no implicit coercion.
- `Schema[Out, In]` typing, a `py.typed` marker, and strict mypy coverage.
- Maturin wheel and sdist builds for the CPython 3.14+ stable ABI, with no
  third-party runtime dependencies.

### Fixed

- Async callbacks, and callbacks returning awaitables, now raise `TypeError`
  instead of being accepted as valid refinement results.
- Defaults are typed and validated as input values, matching runtime behavior
  after a transform.
- A callback raising `ValidationError([])` now produces a `custom` issue
  instead of being treated as success.
