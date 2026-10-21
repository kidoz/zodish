# API reference

This page lists the public schema builders and methods. For behavior details and
examples, follow the links into the guides and explanation pages.

```python
from zodish import Schema, z
```

Schemas are immutable: every fluent method returns a new schema. Operations run
in declaration order, and the order affects results.

## Base constructors

| Group | Constructors |
| --- | --- |
| Strings | `z.string()` |
| Numbers | `z.number()`, `z.integer()` |
| Other values | `z.boolean()`, `z.none()`, `z.unknown()`, `z.date()`, `z.datetime()`, `z.time()`, `z.timedelta()`, `z.decimal()`, `z.bytes()`, `z.path()`, `z.json()`, `z.native_enum(EnumClass)`, `z.instance_of(cls)` |
| Choices | `z.literal(value)`, `z.enum(values)`, `z.union(*schemas)`, `z.discriminated_union(key, options)` |
| Objects | `z.object(shape)`, `z.typed_object(TypedDictClass, shape)` |
| Collections | `z.array(item)`, `z.set(item)`, `z.map(key_schema, value_schema)`, `z.tuple(*items)`, `z.record(value_schema)`, `z.record(key_schema, value_schema)` |
| Composition | `z.intersection(left, right)`, `z.preprocess(fn, schema)`, `z.preprocess_async(fn, schema)`, `z.lazy(factory)` |
| Codecs | `z.codec(input_schema, output_schema, *, decode=fn, encode=fn)`, `z.codec_async(...)` |

`z.date()`, `z.datetime()`, `z.time()`, `z.timedelta()`, `z.decimal()`,
`z.bytes()`, and `z.path()` validate Python objects. They are not string format
checks; use `.date()`, `.time()`, and `.datetime()` on `z.string()` for string
formats.

## String methods

| Method | Purpose |
| --- | --- |
| `.min(n)`, `.max(n)`, `.length(n)` | Length bounds |
| `.regex(pattern)` | Python `re.search` match |
| `.email()`, `.url()`, `.uuid()` | Format checks |
| `.ulid()`, `.cuid()`, `.cuid2()`, `.nanoid()` | Identifier format checks |
| `.jwt()`, `.e164()` | Token and phone-number format checks |
| `.base64()`, `.base64url()` | Base64 syntax checks |
| `.date()`, `.time()`, `.datetime()` | String date/time format checks |
| `.ipv4()`, `.ipv6()`, `.hostname()` | Format checks |
| `.starts_with(s)`, `.ends_with(s)`, `.includes(s)` | Substring predicates |
| `.trim()`, `.lower()`, `.upper()` | Value normalization |
| `.stringbool(...)` | Coerce configured strings to `bool` |

## Number methods

| Method | Purpose |
| --- | --- |
| `.min(n)`, `.max(n)` | Inclusive bounds |
| `.gt(n)`, `.gte(n)`, `.lt(n)`, `.lte(n)` | Exclusive and inclusive aliases |
| `.multiple_of(n)` | Multiple check |
| `.positive()`, `.nonnegative()` | Sign checks |
| `.int()` | Accept integral floats and retain their type |

## Object methods

| Method | Purpose |
| --- | --- |
| `.strict()` | Reject unknown keys |
| `.strip()` | Remove unknown keys (default) |
| `.passthrough()` | Preserve unknown keys |
| `.catchall(schema)` | Validate and preserve unknown keys |
| `.extend(shape)` | Add or replace fields |
| `.pick(*keys)` | Keep selected fields |
| `.omit(*keys)` | Remove selected fields |
| `.partial(*keys)` | Make all or selected fields optional |
| `.required()` | Make every field reject missing input |
| `.keyof()` | Enum schema of the shape keys |

## Collection methods

| Method | Applies to | Purpose |
| --- | --- | --- |
| `.min(n)`, `.max(n)` | Arrays | Item-count bounds |
| `.length(n)` | Arrays | Exact item count |
| `.nonempty()` | Arrays | At least one item |
| `.rest(schema)` | Tuples | Trailing rest schema |
| `.array()` | Any schema | Shorthand for `z.array(schema)` |

`z.set(item)` validates a Python `set` and returns a new set. Element issues use
the iteration index in their paths. Sets are not JSON-representable and cannot
be encoded.

`z.map(key_schema, value_schema)` validates a Python dictionary through the same
native record evaluator as `z.record(key_schema, value_schema)`.

## Choice and composition methods

| Method | Purpose |
| --- | --- |
| `schema.or_(other)` | Union shorthand |
| `schema.and_(other)` | Intersection shorthand |
| `z.union(*schemas)` | Try branches in order |
| `z.discriminated_union(key, options)` | Dispatch on a dictionary key |
| `z.intersection(left, right)` | Validate against both schemas |
| `z.preprocess(fn, schema)` | Convert input before validation |
| `z.preprocess_async(fn, schema)` | Await input conversion before validation |
| `z.lazy(factory)` | Recursive schema; the factory is resolved once per lazy node per parse |

## Methods on every schema

| Method | Purpose |
| --- | --- |
| `.parse(value)` | Return validated output or raise `ValidationError` |
| `.safe_parse(value)` | Return `Success[T]` or `Failure` |
| `.validate(value)` | Return `True` or `False` |
| `.parse_json(data)` | Parse JSON text and validate the decoded value |
| `.safe_parse_json(data)` | Return `Success[T]` or `Failure` for JSON input |
| `.validate_json(data)` | Return whether JSON text parses and validates |
| `.parse_async(value)` | Await parsing, including nested async callbacks |
| `.safe_parse_async(value)` | Return `Success[T]` or `Failure` from async parsing |
| `.validate_async(value)` | Return whether async validation succeeds |
| `.parse_json_async(data)` | Parse JSON text and await nested async callbacks |
| `.safe_parse_json_async(data)` | Return `Success[T]` or `Failure` from async JSON parsing |
| `.encode(value)` | Reverse-validate and encode when the schema is reversible |
| `.safe_encode(value)` | Return `Success[In]` or `Failure` |
| `.optional()` | Accept missing input; not `None` |
| `.nonoptional()` | Reject missing input |
| `.nullable()` | Accept `None`; not missing input |
| `.nullish()` | Accept missing input and `None` |
| `.default(value)` | Supply a fallback for missing input |
| `.default_factory(fn)` | Call `fn()` for missing input |
| `.default_factory_async(fn)` | Await `fn()` for missing input, then validate it |
| `.catch(value_or_factory)` | Fall back on invalid or missing input |
| `.catch_async(factory)` | Await a fallback factory on validation failure |
| `.unwrap()` | Return the tracked inner schema |
| `.brand(NewType)` | Apply a typing-only brand to parsed output |
| `.refine(predicate, message, *, path=())` | Add a custom synchronous check |
| `.refine_async(predicate, message, *, path=())` | Add a check awaiting an awaitable result |
| `.super_refine_async(fn)` | Await returned issues, or `None`/empty for success |
| `.super_refine(fn)` | Return one `Issue`, a list/tuple of issues, or `None` |
| `.transform(fn)` | Convert parsed output synchronously |
| `.transform_async(fn)` | Convert parsed output by awaiting an awaitable |
| `.overwrite(fn)` | Change a value without changing its type |
| `.overwrite_async(fn)` | Await a value change without changing its static type |
| `.pipe(schema)` | Feed output into another schema |
| `.meta(metadata)` | Merge documentation metadata |
| `.describe(text)` | Replace the `description` metadata |
| `.metadata` | Read-only metadata mapping |
| `.to_json_schema(...)` | Export JSON Schema |

Built-in constraints accept a keyword-only `message=`. `.refine()` paths are
relative to the current location, and `.super_refine()` issue paths are also
relative.

## Evaluation modes

Synchronous parsing is the default: `parse`, `safe_parse`, `validate`,
`parse_json`, `safe_parse_json`, `validate_json`, `encode`, and `safe_encode`
return directly.

`parse_async`, `safe_parse_async`, `validate_async`, `parse_json_async`, and
`safe_parse_json_async` provide the asynchronous path. `refine_async`,
`super_refine_async`, `transform_async`, `overwrite_async`,
`default_factory_async`, `catch_async`, and `z.preprocess_async` callbacks may
be `async def` functions or ordinary callables returning awaitables. Async
operations compose with objects, arrays, records, tuples, unions, intersections,
discriminated unions, pipes, wrappers, and synchronous lazy factories.
`z.codec_async` adds awaitable codec callbacks; `.decode_async()`,
`.encode_async()`, and their safe variants use the same session protocol.
Calling a synchronous parser or encoder on a reached async node raises
`TypeError`; an optional, nullable, or earlier union branch may bypass it. Lazy
factories remain synchronous; a factory may return a schema containing async
operations. See [Async support](../explanation/async-support.md) for the
complete status and limitations.

## Coercion

| Builder | Accepted input |
| --- | --- |
| `z.coerce.string()` | `str`, exact `int`, finite exact `float` |
| `z.coerce.number()` | Exact `int`, finite `float`, numeric string |
| `z.coerce.integer()` | `int`, integral float, integer string |
| `z.coerce.boolean()` | `bool`, `0`/`1`, true/false-like strings |

`z.string().stringbool()` returns a Python `bool`. It accepts `truthy`, `falsy`,
`case_sensitive`, and `message` options; it normalizes case by default.

## Codecs

| Builder or method | Purpose |
| --- | --- |
| `z.codec(input_schema, output_schema, *, decode=fn, encode=fn)` | Build a bidirectional conversion |
| `z.codec_async(input_schema, output_schema, *, decode=fn, encode=fn)` | Build a codec with awaitable conversion callbacks |
| `z.codecs.iso_date()`, `iso_time()`, `iso_datetime()` | ISO strings ↔ `date`, `time`, `datetime` |
| `z.codecs.decimal()`, `json_text()` | String ↔ `Decimal` or JSON-compatible values |
| `z.codecs.path()`, `bytes_base64(urlsafe=False)` | String ↔ `Path` or `bytes` |
| `z.codecs.dataclass(Cls, fields=None)` | Encoded dictionary ↔ dataclass instance; field schemas inferred from annotations, with optional overrides |
| `.decode(value)`, `.safe_decode(value)` | Decode; equivalent to `.parse()` and `.safe_parse()` |
| `.decode_async(value)`, `.safe_decode_async(value)` | Await decoding, including async codecs |
| `.encode(value)`, `.safe_encode(value)` | Reverse-validate decoded input and produce encoded output |
| `.encode_async(value)`, `.safe_encode_async(value)` | Await reverse validation and encoding |
| `.input_schema`, `.output_schema` | Encoded and decoded schema references |
| `.optional()`, `.nullable()`, `.nonoptional()`, `.nullish()` | Preserve codec conversion through wrappers |
| `.record()` | Encoded dictionary values ↔ decoded dictionary values |
| `z.codecs.array(codec)` | Encoded list ↔ decoded list |
| `z.codecs.union(*schemas)`, `z.codecs.tuple(*schemas)` | Preserve conversions across unions or fixed tuples; plain schemas act as identity branches |

Decoding validates encoded input, calls `decode`, then validates decoded output.
Encoding validates decoded input, calls `encode`, then validates encoded output.
`z.codec` callbacks are synchronous. `z.codec_async` requires awaitable callbacks;
use `.decode_async()`/`.encode_async()` for that codec. Ordinary callables
returning awaitables are accepted. Async codecs preserve the same input/output
schemas, wrappers, records, arrays, unions, tuples, and dataclass codecs.
Synchronous codec entry points reject async field codecs; use
`.decode_async()`/`.encode_async()` instead.

Encoding is available only for reversible schema trees. Primitives, constraints,
codecs, optional/nullable/nonoptional wrappers, and structural containers are
reversible. Transforms, overwrites, refinements, coercions and `stringbool`,
defaults and `default_factory`, catches, pipes, preprocessors, and lazy schemas
are one-way, so `.encode()` and `.encode_async()` raise `TypeError` when they
occur. Async codec callbacks participate in the reverse graph; plain schemas and
codecs can use `.encode_async()`, and synchronous reverse graphs complete through
the same session protocol.

## JSON Schema export

| Call | Purpose |
| --- | --- |
| `z.to_json_schema(schema, *, target=..., io=..., unrepresentable=...)` | Export a schema |
| `schema.to_json_schema(...)` | Method form |

See the [JSON Schema reference](json-schema.md) for options and the support
matrix.

## Metadata and introspection

Metadata is a read-only mapping. `.meta()` merges keys and overrides existing
ones; `.describe()` replaces `description`. Metadata follows value-preserving
derivations such as constraints, refinements, transforms, wrappers, and object
structural methods. Collection and composition constructors start with empty
metadata. Metadata does not affect validation, equality, or hashing.

Container and composition schemas retain their children:

| Attribute | Schema |
| --- | --- |
| `.item` | Array item schema |
| `.value_schema` | Record value schema |
| `.items` | Tuple item schemas |
| `.options` | Union options (tuple) or discriminated-union options (mapping) |

## Static typing

`Schema[Out, In]` tracks parsed output and accepted input types for primitives,
homogeneous collections (including sets and maps), transforms, preprocessors,
and wrappers. `In` defaults
to `Out`, so `Schema[T]` is valid when input and output types match.
`CodecSchema[Out, In]` follows the same convention: `Out` is the decoded type and
`In` is the encoded type.

Type checkers cannot generally derive a new `TypedDict` from a dictionary of
schema instances. Plain objects return `dict[str, Any]`; use
`z.typed_object(TypedDictClass, shape)` for a statically typed dictionary
result. `z.tuple()` infers a fixed-length tuple type for up to four items; larger
tuples and tuples with a rest schema return `tuple[Any, ...]`. Use
`z.codecs.dataclass(Cls, fields=...)` or a transform to produce a statically
typed dataclass result. Unions infer up to four alternatives; larger unions fall
back to `Any`. `.brand(NewType)` changes only the static output type and is a
runtime no-op. There is no type-checker plugin or generated model API.

## Related

- [Errors reference](errors.md)
- [JSON Schema reference](json-schema.md)
- [Type semantics](../explanation/type-semantics.md)
