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
| Objects | `z.object(shape)` |
| Collections | `z.array(item)`, `z.tuple(*items)`, `z.record(value_schema)`, `z.record(key_schema, value_schema)` |
| Composition | `z.intersection(left, right)`, `z.preprocess(fn, schema)`, `z.lazy(factory)` |
| Codecs | `z.codec(input_schema, output_schema, *, decode=fn, encode=fn)` |

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

## Choice and composition methods

| Method | Purpose |
| --- | --- |
| `schema.or_(other)` | Union shorthand |
| `schema.and_(other)` | Intersection shorthand |
| `z.union(*schemas)` | Try branches in order |
| `z.discriminated_union(key, options)` | Dispatch on a dictionary key |
| `z.intersection(left, right)` | Validate against both schemas |
| `z.preprocess(fn, schema)` | Convert input before validation |
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
| `.encode(value)` | Reverse-validate and encode when the schema is reversible |
| `.safe_encode(value)` | Return `Success[In]` or `Failure` |
| `.optional()` | Accept missing input; not `None` |
| `.nonoptional()` | Reject missing input |
| `.nullable()` | Accept `None`; not missing input |
| `.nullish()` | Accept missing input and `None` |
| `.default(value)` | Supply a fallback for missing input |
| `.default_factory(fn)` | Call `fn()` for missing input |
| `.catch(value_or_factory)` | Fall back on invalid or missing input |
| `.unwrap()` | Return the tracked inner schema |
| `.refine(predicate, message, *, path=())` | Add a custom check |
| `.super_refine(fn)` | Return one `Issue`, a list/tuple of issues, or `None` |
| `.transform(fn)` | Convert parsed output |
| `.overwrite(fn)` | Change a value without changing its type |
| `.pipe(schema)` | Feed output into another schema |
| `.meta(metadata)` | Merge documentation metadata |
| `.describe(text)` | Replace the `description` metadata |
| `.metadata` | Read-only metadata mapping |
| `.to_json_schema(...)` | Export JSON Schema |

Built-in constraints accept a keyword-only `message=`. `.refine()` paths are
relative to the current location, and `.super_refine()` issue paths are also
relative.

## Synchronous evaluation

All schema methods and callbacks are synchronous. There is no async API:
`parse`, `safe_parse`, `validate`, `parse_json`, `safe_parse_json`,
`validate_json`, `encode`, and `safe_encode` return directly, and async
callbacks are rejected with `TypeError`. See
[Async support](../explanation/async-support.md).

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
| `z.codecs.iso_date()`, `iso_time()`, `iso_datetime()` | ISO strings ↔ `date`, `time`, `datetime` |
| `z.codecs.decimal()`, `json_text()` | String ↔ `Decimal` or JSON-compatible values |
| `z.codecs.path()`, `bytes_base64(urlsafe=False)` | String ↔ `Path` or `bytes` |
| `z.codecs.dataclass(Cls, fields=None)` | Encoded dictionary ↔ dataclass instance; field schemas inferred from annotations, with optional overrides |
| `.decode(value)`, `.safe_decode(value)` | Decode; equivalent to `.parse()` and `.safe_parse()` |
| `.encode(value)`, `.safe_encode(value)` | Reverse-validate decoded input and produce encoded output |
| `.input_schema`, `.output_schema` | Encoded and decoded schema references |
| `.optional()`, `.nullable()`, `.nonoptional()`, `.nullish()` | Preserve codec conversion through wrappers |
| `.record()` | Encoded dictionary values ↔ decoded dictionary values |
| `z.codecs.array(codec)` | Encoded list ↔ decoded list |
| `z.codecs.union(*schemas)`, `z.codecs.tuple(*schemas)` | Preserve conversions across unions or fixed tuples; plain schemas act as identity branches |

Decoding validates encoded input, calls `decode`, then validates decoded output.
Encoding validates decoded input, calls `encode`, then validates encoded output.
Callbacks must be synchronous.

Encoding is available only for reversible schema trees. Primitives, constraints,
codecs, optional/nullable/nonoptional wrappers, and structural containers are
reversible. Transforms, overwrites, refinements, coercions and `stringbool`,
defaults and `default_factory`, catches, pipes, preprocessors, and lazy schemas
are one-way, so `.encode()` raises `TypeError` when they occur.

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
homogeneous collections, transforms, preprocessors, and wrappers. `In` defaults
to `Out`, so `Schema[T]` is valid when input and output types match.
`CodecSchema[Out, In]` follows the same convention: `Out` is the decoded type and
`In` is the encoded type.

Type checkers cannot generally derive a new `TypedDict` from a dictionary of
schema instances. Objects return `dict[str, Any]`. `z.tuple()` infers a
fixed-length tuple type for up to four items; larger tuples and tuples with a
rest schema return `tuple[Any, ...]`. Use `z.codecs.dataclass(Cls, fields=...)`
or a transform to produce a statically typed dataclass result. Unions infer up to four
alternatives; larger unions fall back to `Any`. There is no type-checker plugin
or generated model API.

## Related

- [Errors reference](errors.md)
- [JSON Schema reference](json-schema.md)
- [Type semantics](../explanation/type-semantics.md)
