# Async support

Zodish supports asynchronous validation for async callbacks. Synchronous
validation remains the default and is unchanged.

## Status

| Question | Answer |
| --- | --- |
| Is async validation stable? | Yes, as of 0.5.0. |
| Is there `await schema.parse_async(value)`? | Yes. |
| Is there `await schema.safe_parse_async(value)`? | Yes. |
| Can async callbacks be used? | Through `refine_async`, `super_refine_async`, `transform_async`, `overwrite_async`, `default_factory_async`, `catch_async`, and `z.preprocess_async`. |
| Can async operations be nested in objects, arrays, sets, maps, records, tuples, unions, intersections, discriminated unions, pipes, or wrappers? | Yes. |
| Can synchronous builders such as `refine`, `overwrite`, `default_factory`, `catch`, `preprocess`, lazy, or codec callbacks be async? | No. |
| Does `parse_json_async` exist? | Yes, with `safe_parse_json_async`. |
| Does `validate_async` exist? | Yes. |
| Does `encode_async` exist? | Yes, with `safe_encode_async`. |
| Can codec callbacks be async? | Yes, through `z.codec_async` and `.decode_async()`/`.encode_async()`. |
| Does Zodish create event loops or tasks? | No. |

Synchronous `parse`, `safe_parse`, `validate`, `parse_json`, `encode`, and
`safe_encode` remain available. Calling a synchronous parser on a schema that
reaches an async operation raises `TypeError`; use `parse_async` instead. An
optional or nullable short circuit and an earlier successful union branch may
bypass async code entirely, in which case synchronous parsing remains valid.

## Async callbacks

`refine_async`, `super_refine_async`, `transform_async`, `overwrite_async`,
`default_factory_async`, `catch_async`, and `z.preprocess_async` accept an
`async def` callback or an ordinary callable returning an awaitable:

```python
from zodish import z

async def email_is_available(email: str) -> bool:
    return not await repository.email_exists(email)

registration = z.object({
    "email": z.string().email().refine_async(
        email_is_available,
        "Email is already registered",
    ),
    "name": z.string().min(1),
})

result = await registration.safe_parse_async(payload)
```

For refinements, transforms, and overwrites, the child schema is validated
first and its parsed output is passed to the async callback. `preprocess_async`
runs before the child schema. `default_factory_async` runs only for missing
input. `catch_async` runs only after a native validation failure. Each
callback's awaitable is awaited once in the caller's task; Zodish does not
create tasks, parallelize fields or branches, or manage an event loop.

`transform_async` replaces the parsed output with the awaited value, following
the same one-way semantics as `transform`. `overwrite_async` does the same
without changing its static type. The result is not recursively validated or
promise-flattened.

`z.preprocess_async(fn, schema)` awaits `fn(input)` and validates the result
through `schema`. `default_factory_async(factory)` awaits a missing-input
factory and then validates its result through the child schema.
`catch_async(factory)` awaits a zero-argument fallback after a native validation
failure; its resolved value is returned without re-validation, matching
`.catch(callable)`. Constant fallbacks use `.catch(value)`.

`super_refine_async` awaits an `Issue`, a list or tuple of issues, or `None`.
`None` and an empty collection succeed. Returned issue paths are relative to the
callback's current location and are prefixed with that path; a callback-raised
`ValidationError` keeps the paths it supplied.

## Composition

Async callbacks can appear anywhere a schema can appear. Validation keeps the
usual declaration order:

- object fields, array items, set items, map entries, tuple items, and record values run sequentially;
- sibling validation failures aggregate in order and include nested paths;
- unions try branches in order and stop at the first success;
- discriminated unions evaluate only the selected branch;
- intersections evaluate left and then right, including after an ordinary
  programming exception;
- pipes evaluate their first schema before the second;
- `optional`, `nullable`, `nonoptional`, `default`, `default_factory`,
  `default_factory_async`, `catch`, `catch_async`, and `lazy` keep their
  existing placement-dependent behavior;
- synchronous callbacks nested among async callbacks run in place.

An ordinary programming exception aborts a union and a container. `.catch()` and
`.catch_async()` still handle only native validation failures, not programming
errors. Cancellation and other control `BaseException` subclasses propagate
immediately; inside an async intersection they also stop the remaining side.

Callbacks run in the caller's task. Zodish does not schedule independent fields
or array elements concurrently. If independent I/O can be batched, do so inside
one object-level async callback using application-owned concurrency.

## Async codecs and encoding

`z.codec_async(input_schema, output_schema, *, decode=fn, encode=fn)` builds a
codec whose callbacks may be `async def` functions or ordinary callables
returning awaitables. It validates encoded input, awaits `decode`, then
validates decoded output. The reverse direction validates decoded input,
awaits `encode`, then validates encoded output.

```python
lookup = z.codec_async(
    z.string(),
    z.integer(),
    decode=record_id_for_text,
    encode=text_for_record_id,
)
value = await lookup.decode_async("42")
encoded = await lookup.encode_async(42)
```

`.decode_async()`/`.safe_decode_async()` are available on codecs;
`.encode_async()`/`.safe_encode_async()` are available on all schemas.
`encode_async` walks the same reversible graph as `encode`, using a native
asynchronous session when the reverse graph contains async codecs or callbacks.
Sync codecs and plain reversible schemas also work through the async entry
points. The codec wrappers, record/array/union/tuple codec combinators, and
ordinary containers preserve both directions for async codecs. Dataclass codecs
await async field codecs through `.decode_async()`/`.encode_async()` and preserve
field issue prefixes and dataclass defaults.

## Errors and cancellation

- A false `refine_async` result produces the usual `custom` issue with the
  configured message and relative path.
- A callback-raised `ValidationError` is normalized at that callback and may be
  aggregated by its parent, inspected as a union branch failure, or caught by
  `.catch()`. An empty raised error becomes one `custom` issue at the callback
  path.
- Other exceptions propagate unchanged, including through synchronous parents.
- `safe_parse_async` catches only `ValidationError`.
- Cancellation, `KeyboardInterrupt`, `SystemExit`, and other control exceptions
  propagate as the original instance.
- Async generator callbacks and callbacks returning async generators are
  rejected.
- A rejected or explicitly abandoned coroutine is closed by the normal
  `close()` cleanup path.

Each parse uses its own native session, so concurrent parses of one immutable
schema keep independent validation state and lazy caches. A session is bound to
the thread that starts it; resuming from another thread is a protocol error.
Concurrent coroutine tasks on the same event-loop thread are supported.
GIL-enabled CPython 3.14+ remains the default distribution target; version-specific
`cp314t`/`cp315t` free-threaded wheels are published for Linux x86_64/aarch64
glibc, macOS x86_64/arm64, and Windows x64.

## Current limitations

The following remain outside the current async surface:

- Lazy factories stay synchronous. A factory may still return a schema that
  contains async operations, so recursive async schemas are supported. Async
  schema construction itself is not.

JSON Schema export still treats async callback nodes as unrepresentable
(`unrepresentable="throw"` or `"any"`); async codecs export their input and
output schemas. Encoding through the synchronous `.encode()` entry point raises
`TypeError` when the reverse graph reaches an async callback; use
`.encode_async()` instead.

`parse_json_async` accepts the same `str`, `bytes`, or `bytearray` input as
`parse_json`. Schemas covered by the native JSON subset keep that fast path;
other schemas use standard JSON decoding followed by asynchronous validation.
Invalid JSON raises the usual `invalid_format` issue with a `position`.
`safe_parse_json_async` catches only `ValidationError`, including JSON decoding
failures. `validate_async` mirrors `validate`: it returns `bool` and lets
non-validation callback exceptions propagate.

## Implementation

The asynchronous implementation uses a native resumable session. Rust retains
the validation frame stack, container snapshots, paths, issues, lazy cache, and
callbacks that must be resumed. Synchronous subtrees without async descendants
continue to use the existing native evaluator and fast paths. The Python adapter
only awaits the requested callback result and resumes the session; it never
walks schemas or validates values.

Async fields and branches are measured separately from synchronous parsing in
`benchmarks/bench_async.py`.

## Related

- [Transform and refine values](../how-to/transform-and-refine.md)
- [Type semantics](type-semantics.md)
- [API reference](../reference/api.md)
