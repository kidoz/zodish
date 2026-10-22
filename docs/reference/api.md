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
| `.email()`, `.url()`, `.http_url()`, `.uuid()` | Format checks |
| `.ulid()`, `.cuid()`, `.cuid2()`, `.nanoid()` | Identifier format checks |
| `.jwt()`, `.e164()`, `.hex()` | Token, phone-number, and hex syntax checks |
| `.base64()`, `.base64url()` | Base64 syntax checks |
| `.date()`, `.time()`, `.datetime()`, `.duration()` | String date/time and ISO duration checks |
| `.ipv4()`, `.ipv6()`, `.cidrv4()`, `.cidrv6()`, `.hostname()` | Address and format checks |
| `.starts_with(s)`, `.ends_with(s)`, `.includes(s)` | Substring predicates |
| `.trim()`, `.lower()`, `.upper()` | Value normalization |
| `.stringbool(...)` | Coerce configured strings to `bool` |

`.regex()` delegates to Python `re.search`; an author-supplied pattern can
backtrack exponentially on crafted input. The built-in format helpers use fixed
patterns and do not take pattern input.

`.http_url()` accepts only `http` and `https` schemes. `.hex()` accepts one or
more hexadecimal digits and does not require an even length. `.cidrv4()` and
`.cidrv6()` validate address syntax and the prefix range; host bits may be set.
`.duration()` accepts an optional sign, weeks, days, and time components.
Calendar years and months are rejected because `timedelta` cannot represent
them exactly.

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
| `.wire(name)` | Field marker: accept `name` on input, emit the shape key |

`.wire(name)` applies to a field schema inside `z.object()` or
`z.typed_object()`. The shape key is the output name; the wire name is the
input name. Wire names must be unique and must not collide with another field's
input name. Input validation issue paths use the wire name; typed-object
required-output issues use the shape key. `.keyof()`, `.pick()`, `.omit()`,
`.extend()`, and typed-object keys use shape keys. Encoding and `io="input"`
JSON Schema use wire names; `io="output"` uses shape keys. With
`.passthrough()` or `.catchall()`, an unknown input key that equals a shape key
does not overwrite the validated field. A discriminated-union discriminator
cannot have a wire name.

## Collection methods

| Method | Applies to | Purpose |
| --- | --- | --- |
| `.min(n)`, `.max(n)` | Arrays, sets, `z.bytes()` | Item-count, cardinality, or byte-count bounds |
| `.length(n)` | Arrays, sets, `z.bytes()` | Exact count |
| `.nonempty()` | Arrays | At least one item |
| `.rest(schema)` | Tuples | Trailing rest schema |
| `.array()` | Any schema | Shorthand for `z.array(schema)` |

`z.set(item)` validates a Python `set` and returns a new set. Element issues use
the iteration index in their paths. Reverse encoding returns a list, JSON Schema
export uses an array with `uniqueItems: true`, and `.min()`, `.max()`, and
`.length()` map to array length bounds. `parse_json` still requires Python set
input; JSON array-to-set conversion is not implemented.

`z.map(key_schema, value_schema)` validates a Python dictionary through the same
native record evaluator as `z.record(key_schema, value_schema)`. It is
reversibly encodable when both key and value schemas are reversible.

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

Non-discriminated unions evaluate every failing branch. Deeply nested unions can
grow exponentially; prefer `z.discriminated_union()` when branches share a
literal discriminator.

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

`parse`, `safe_parse`, `parse_json`, `safe_parse_json`, the async parse
variants, and `decode`, `safe_decode`, `decode_async`, and
`safe_decode_async` accept a keyword-only `error=callable`.
The mapper receives each `Issue` when issues are materialized and returns the
replacement `message`. It does not run during successful validation or
`validate()`; see [Errors reference](errors.md#per-parse-message-mapping).

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
| `z.codecs.iso_duration()` | ISO 8601 duration string ↔ `timedelta` |
| `z.codecs.decimal()`, `json_text()` | String ↔ `Decimal` or JSON-compatible values |
| `z.codecs.path()`, `bytes_base64(urlsafe=False)` | String ↔ `Path` or `bytes` |
| `z.codecs.dataclass(Cls, fields=None)` | Encoded dictionary ↔ dataclass instance; infers annotations, `Annotated[T, schema]`, and aliases with ordinary type parameters and defaults; `fields=` overrides inference |
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

Dataclass `Annotated` metadata accepts one schema or codec; other metadata is
ignored. Multiple schemas raise `TypeError`, including across alias boundaries.
The annotated type describes decoded output; its agreement with the schema is
the caller's responsibility. Generic aliases substitute ordinary type
parameters, including defaults that refer to earlier parameters. Missing
arguments without defaults and excess arguments raise `TypeError`. Bounds and
constraints are checked statically. Recursive and unresolved aliases and
variadic parameters require explicit field definitions. Parameterized dataclass
classes such as `Box[int]` remain unsupported. Ordinary dataclass construction
does not validate; use `@z.dataclass` for validated construction. See
[Use codecs](../how-to/use-codecs.md).

Encoding is available only for reversible schema trees. Primitives, constraints,
codecs, optional/nullable/nonoptional wrappers, and structural containers are
reversible. Transforms, overwrites, refinements, coercions and `stringbool`,
defaults and `default_factory`, catches, pipes, preprocessors, and lazy schemas
are one-way, so `.encode()` and `.encode_async()` raise `TypeError` when they
occur. Async codec callbacks participate in the reverse graph; plain schemas and
codecs can use `.encode_async()`, and synchronous reverse graphs complete through
the same session protocol.

## Validated dataclasses

| Call | Purpose |
| --- | --- |
| `@z.dataclass` | Validate and normalize ordinary construction |
| `@z.dataclass()` | Decorator form with default options |
| `@z.dataclass(frozen=True, slots=True, kw_only=True, ...)` | Pass dataclass options through |
| `z.dataclass(ExistingDataclass)` | Wrap an existing supported dataclass |

Constructor arguments are decoded values typed by the field annotations.
Encoded input and reverse encoding use `z.codecs.dataclass(Cls)`. Defaults and
default factories are validated; a factory runs once for an omitted field and
not for supplied fields. Missing required, extra, and incorrectly bound
arguments raise `TypeError`; field and default failures raise `ValidationError`
with aggregated field paths.

Explicit `Annotated[T, schema]` field schemas must produce `T`. Clearly
incompatible output kinds fail at decoration with `TypeError`; other explicit
schemas receive a native output guard that raises `TypeError` on mismatch.
Inferred schemas are trusted. An optional field schema without a dataclass
default, a `MISSING` default, `preprocess` schemas, `transform`, `coerce`,
`stringbool`, and `pipe` schemas, lazy schemas whose async capability is not
statically known, async construction, custom `__init__`, `__new__`, and
`__setattr__`, and dataclass-level `init=False` are rejected at decoration. Use
`.overwrite()` for type-preserving normalization or a codec for type-changing
conversions.

`InitVar` parameters are validated, passed to `__post_init__` in declaration
order, and are not stored or encoded. InitVar defaults are validated. An
`init=False` field with a default or `default_factory` is validated and assigned
before `__post_init__`; an `init=False` field without a default may be assigned
by `__post_init__` and is not validated. Post-init mutations are not revalidated.
Codec decode accepts InitVar input and applies `init=False` defaults; encode
omits both. Because InitVar values are not stored, `dataclasses.replace()` on a
class with a required InitVar follows stdlib behavior and cannot supply it.

A validated class may inherit from one dataclass base chain (ordinary or
validated) plus non-dataclass mixins. Inherited fields, defaults, factories,
overrides, `InitVar`, `init=False` fields, and `__post_init__` are validated
with the subclass's annotations. Multiple dataclass bases, generic or
parameterized bases, and a base with the `init=False` option or a custom
`__init__` are rejected. An undecorated subclass is not validated; the codec
rejects it, and stdlib `@dataclasses.dataclass` on the subclass does not restore
validation. Decorate the subclass with `@z.dataclass`.

Supported options include `frozen`, `slots`, `weakref_slot=True` with slots,
`kw_only`, `repr`, `eq`, `order`, `unsafe_hash`, and `match_args`. A
zero-argument `__post_init__` runs after validation; fields it mutates are not
revalidated. `dataclasses.replace()` revalidates; assignment, `copy`, `pickle`,
and direct `object.__new__` bypass validation.

`@z.dataclass` uses `dataclass_transform`, so constructor arguments and
attributes are statically typed. Literal `frozen=True` and `kw_only=True` are
honored by type checkers; options computed at runtime are not. Decorated classes
use a dedicated codec decode path that validates supplied fields and omitted
defaults once, guards explicit override outputs, and assembles without calling
the validating initializer. Ordinary stdlib dataclass codecs keep their existing
behavior.

For a decorated class in `z.codecs.dataclass(Cls, fields=...)`, the class
annotation and guard still apply to override outputs, while `fields=` controls
the encoded/decoded conversion. See
[Validate direct construction](../how-to/use-codecs.md#validate-direct-construction).

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
metadata. Codec combinators follow the same rule: `z.codecs.array`,
`codec.record()`, `z.codecs.union`, and `z.codecs.tuple` start empty, while
`optional`, `nonoptional`, `nullable`, and `nullish` preserve the wrapped
codec's metadata. Metadata does not affect validation, equality, or hashing.

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
typed dataclass result. `@z.dataclass` additionally gives the constructor
`dataclass_transform` typing, so constructor arguments and attributes follow the
declared annotations. Unions infer up to four alternatives; larger unions fall
back to `Any`. `.brand(NewType)` changes only the static output type and is a
runtime no-op. There is no type-checker plugin or generated model API.

Typed objects check required key presence after field validation, including
async callbacks. A required field returning `MISSING` produces a `required`
issue at its field path; defaults and factories may supply missing input.
`None` and optional keys follow their field schemas. Policy methods preserve
these checks. JSON Schema requires those keys in output and permits defaulted
input keys to be omitted. Field schemas must produce the declared value types.

## Related

- [Errors reference](errors.md)
- [JSON Schema reference](json-schema.md)
- [Type semantics](../explanation/type-semantics.md)
