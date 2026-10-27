# Zodish

[![tests](https://github.com/kidoz/zodish/actions/workflows/tests.yml/badge.svg)](https://github.com/kidoz/zodish/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/zodish)](https://pypi.org/project/zodish/)
[![Python](https://img.shields.io/pypi/pyversions/zodish)](https://pypi.org/project/zodish/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/kidoz/zodish/blob/main/LICENSE)

A validation library (written in Rust via PyO3) for **CPython 3.14+** (tested on 3.14 and 3.15), with a typed Python API inspired by [Zod](https://zod.dev/). Synchronous parsing is the default; composable async refinements and transforms are supported through explicit async entry points.

Handles: **strings, numbers, integers, booleans, none, unknown, literals, enums, objects, arrays, sets, maps, tuples, records, unions, discriminated unions, intersections, optional/nullable/default wrappers, coercion, stringbool, bidirectional codecs, refinements, transforms, pipes, recursive schemas, metadata, and JSON Schema export** — with immutable schemas, ordered operations, aggregated issues, and native-only evaluation. Zodish is an independent implementation of a subset of Zod's API.

## Documentation

Detailed documentation follows the [Diátaxis](https://diataxis.fr/) framework and lives in [`docs/`](docs/README.md):

- [Tutorials](docs/tutorials/get-started.md) — learn by building a schema step by step.
- [How-to guides](docs/how-to/validate-data.md) — solve a specific validation task.
- [Reference](docs/reference/api.md) — APIs, error codes, and JSON Schema support.
- [Explanation](docs/explanation/architecture.md) — design decisions and semantics.
- [Examples](examples/README.md) — runnable API, settings, event, model, and async programs.

## Installation

```bash
pip install zodish
```

**Build from source** (requires Rust 1.85+ and CPython 3.14+):

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install .
python examples/quickstart.py
```

On Windows, activate the environment with `.venv\Scripts\Activate.ps1` in PowerShell. The remaining commands use the environment's `python`.

There are no third-party Python runtime dependencies. Source builds use maturin and PyO3 and download build dependencies unless they are already cached. Installing a compatible prebuilt wheel does not require Rust. Official GIL wheels use the CPython 3.14+ stable ABI; version-specific free-threaded `cp314t`/`cp315t` wheels are available for Linux x86_64/aarch64 glibc, macOS x86_64/arm64, and Windows x64. Wheels must match the operating system and architecture; an Apple Silicon macOS wheel cannot be used on Linux or Windows. Free-threaded musllinux wheels are not published.

## Quick Start

```python
from zodish import z

user_schema = z.object({
    "name": z.string().trim().min(1),
    "email": z.string().trim().lower().email(),
    "age": z.integer().min(18),
    "role": z.enum(["user", "admin"]).default("user"),
    "nickname": z.string().optional(),
}).strict()

user = user_schema.parse({
    "name": " Alex ", "email": "ALEX@example.com", "age": 30,
})
# {"name": "Alex", "email": "alex@example.com", "age": 30, "role": "user"}

result = user_schema.safe_parse({"name": "", "email": "bad", "age": 12})
if result.success:
    print(result.data)
else:
    print(result.error.to_dict())
    # {"issues": [{"code": "too_small", "minimum": 1, ...}, ...]}
```

`parse` returns validated output or raises `ValidationError`. `safe_parse` returns a `Success[T] | Failure` discriminated union. Both validate nested objects and collect sibling errors, including array indices in error paths. A union failure includes each branch's issues in `branches`. `parse_json`, `safe_parse_json`, and `validate_json` apply the same schema directly to JSON `str`, `bytes`, or `bytearray` input. `parse_async`, `safe_parse_async`, and `validate_async` await async refinements and transforms anywhere in a schema; `parse_json_async` and `safe_parse_json_async` do the same for JSON text. See [Async support](docs/explanation/async-support.md).

## Errors

Issues have a `code`, a `message`, a tuple `path`, and structured `params`. Paths contain object keys and collection indices. Codes include `too_small`, `too_big`, `not_multiple_of`, `invalid_type`, `invalid_value`, `invalid_format`, `invalid_key`, `unrecognized_key`, `invalid_length`, `invalid_union`, `required`, and `custom`. Use `to_dict()` for a JSON-serializable representation; params are merged into each serialized issue.

Parse entry points accept a keyword-only `error=callable` that maps each `Issue` to a replacement `message`. It runs only when issue data is materialized, so successful parses never call it; `validate()` returns a boolean without materializing issues and does not accept `error=`. See [per-parse message mapping](docs/reference/errors.md#per-parse-message-mapping).

```python
import json
from zodish import ValidationError, z

schema = z.object({"users": z.object({"age": z.integer().min(18)}).array()})
result = schema.safe_parse({"users": [{"age": 12}]})
assert not result.success
issue = result.error.issues[0]
assert issue.path == ("users", 0, "age")
assert issue.code == "too_small"
assert issue.params == {"minimum": 18, "inclusive": True, "origin": "number"}
print(json.dumps(result.error.to_dict(), indent=2))

try:
    schema.parse({"users": [{"age": "twelve"}]})
except ValidationError as error:
    assert error.issues[0].code == "invalid_type"
    print(error.flatten())   # form_errors and field_errors
    print(error.treeify())   # nested errors, properties, and items
    print(error.prettify())  # human-readable lines
```

## API Overview

| Schema | Constructors / methods |
| --- | --- |
| Strings | `z.string()`, `.min(n)`, `.max(n)`, `.length(n)`, `.regex(pattern)`, `.email()`, `.url()`, `.http_url()`, `.uuid()`, `.ulid()`, `.cuid()`, `.cuid2()`, `.nanoid()`, `.jwt()`, `.e164()`, `.hex()`, `.base64()`, `.base64url()`, `.date()`, `.time()`, `.datetime()`, `.duration()`, `.ipv4()`, `.ipv6()`, `.cidrv4()`, `.cidrv6()`, `.hostname()`, `.starts_with(s)`, `.ends_with(s)`, `.includes(s)`, `.trim()`, `.lower()`, `.upper()` |
| Numbers | `z.number()`, `z.integer()`, `.min(n)`, `.max(n)`, `.gt(n)`, `.gte(n)`, `.lt(n)`, `.lte(n)`, `.multiple_of(n)`, `.positive()`, `.nonnegative()`, `.int()` |
| Other values | `z.boolean()`, `z.none()`, `z.unknown()`, `z.date()`, `z.datetime()`, `z.time()`, `z.timedelta()`, `z.decimal()`, `z.bytes()`, `z.path()`, `z.json()`, `z.native_enum(EnumClass)`, `z.instance_of(cls)` |
| Choices | `z.literal(value)`, `z.enum(values)`, `z.union(*schemas)`, `z.discriminated_union(key, options)`, `schema.or_(other)` |
| Objects | `z.object(shape)`, `z.typed_object(TypedDictClass, shape)`, field `.wire(name)`, `.wire_names(names)`, `.strict()`, `.strip()`, `.passthrough()`, `.catchall(schema)`, `.extend(shape)`, `.pick(*keys)`, `.omit(*keys)`, `.partial(*keys)`, `.required()`, `.keyof()` |
| Collections | `z.array(item)` / `item.array()`, array `.min(n)`, `.max(n)`, `.length(n)`, `.nonempty()`, `z.set(item)` with `.min(n)`, `.max(n)`, `.length(n)`, `z.map(key_schema, value_schema)`, `z.tuple(*items)` / `.rest(schema)`, `z.record(value_schema)` / `z.record(key_schema, value_schema)`, `z.bytes()` with `.min(n)`, `.max(n)`, `.length(n)` |
| Every schema | `.parse(value)`, `.safe_parse(value)`, `.validate(value)`, `.parse_json(data)`, `.safe_parse_json(data)`, `.validate_json(data)`, `.parse_async(value)`, `.safe_parse_async(value)`, `.validate_async(value)`, `.parse_json_async(data)`, `.safe_parse_json_async(data)`, `.encode(value)`, `.safe_encode(value)`, `.optional()`, `.nonoptional()`, `.nullable()`, `.nullish()`, `.default(value)`, `.default_factory(fn)`, `.default_factory_async(fn)`, `.catch(value_or_factory)`, `.catch_async(factory)`, `.unwrap()`, `.brand(NewType)`, `.refine(predicate, message, *, path=())`, `.refine_async(predicate, message, *, path=())`, `.super_refine(fn)`, `.super_refine_async(fn)`, `.transform(fn)`, `.transform_async(fn)`, `.overwrite(fn)`, `.overwrite_async(fn)`, `.pipe(schema)`, `.meta(metadata)`, `.describe(text)`, `.metadata`, `.to_json_schema(...)` |
| Codecs | `z.codec(input_schema, output_schema, *, decode=fn, encode=fn)`, `z.codec_async(...)`, `.decode(value)`, `.safe_decode(value)`, `.decode_async(value)`, `.safe_decode_async(value)`, `.encode(value)`, `.safe_encode(value)`, `.encode_async(value)`, `.safe_encode_async(value)`, `.input_schema`, `.output_schema`, `.optional()`, `.nullable()`, `.nonoptional()`, `.nullish()`, `.record()`, `z.codecs.array(codec)`, `z.codecs.union(*schemas)`, `z.codecs.tuple(*schemas)` |
| Built-in codecs | `z.codecs.iso_date()`, `iso_time()`, `iso_datetime()`, `iso_duration()`, `decimal()`, `json_text()`, `path()`, `bytes_base64(urlsafe=False)`, `dataclass(Cls, fields=None)` |
| Coercion | `z.coerce.string()`, `z.coerce.number()`, `z.coerce.integer()`, `z.coerce.boolean()`, `z.string().stringbool(...)` |
| JSON Schema | `z.to_json_schema(schema, *, target=..., io=..., unrepresentable=...)` |
| Composition | `z.intersection(left, right)` / `schema.and_(other)`, `z.preprocess(fn, schema)`, `z.preprocess_async(fn, schema)`, `z.lazy(factory)` |
| Annotation workflows | `z.adapter(annotation)`, `@z.validate_call`, `@z.model_validator` |

Parse and async variants accept keyword-only `context=`. Context-aware callbacks use `.refine_with_context()`, `.super_refine_with_context()`, `.transform_with_context()`, and their async counterparts, receiving `(value, context)` without changing existing callback signatures.

Built-in constraints accept a keyword-only `message=`. `.refine()` accepts a relative `path=`, and `.super_refine()` may return one `Issue`, a list or tuple of `Issue` objects, or `None`; returned issue paths are relative to the current location. `z.enum()` reports one `invalid_value` issue rather than a union branch.

Object fields can declare an input-only wire name: `z.object({"user_name": z.string().wire("userName")})` accepts `userName`, returns `user_name`, and encodes back to `userName`. Use `.wire_names(("displayName", "name"))` to accept multiple aliases; the first is canonical for encoding, and supplying several together reports `duplicate_key`. Missing and `None` behavior comes from the field schema. Wire names must be unique across the object; `.keyof()` and shape methods use canonical keys, and the discriminated-union key cannot be aliased.

Dataclass codec fields also support `.wire(name)` and `.wire_names(names)`, applied after the field's codec
and wrappers. Direct construction uses Python attribute names and decoded
values; codec decoding accepts aliases and encoding emits the canonical name.

Schemas are immutable: fluent methods return a new schema. Operations execute in declaration order: `.trim().min(1)` validates the trimmed string. Structural object methods operate on the shape. Apply `.refine()` or `.super_refine()` after structural edits; they return a general `Schema[T]`, as do transforms, pipes, and optional/default wrappers. Type-specific methods should therefore precede these wrappers.

## Missing, None, and Defaults

```python
from zodish import MISSING, z

z.string().optional().parse() is MISSING  # True
z.string().nullable().parse(None)        # None
z.string().default("guest").parse()      # "guest"
z.string().default("guest").safe_parse(None).success  # False
```

* Fields are required by default, including `z.unknown()`.
* `optional()` permits missing input; it does not permit `None`.
* `nullable()` permits `None`; it does not permit missing input.
* `nullish()` permits both.
* Missing optional object fields are omitted from output.
* Defaults apply only to missing input. A default is copied and then passed through
  the schema, so constraints and transforms also apply to defaults. This differs
  from Zod 4's short-circuit default behavior.
* `default_factory` runs only for missing input. A default inside an outer
  `optional()` is skipped for missing input because the outer wrapper short-circuits.

Wrapper order affects missing values:

```python
from zodish import MISSING, z

assert z.string().default("guest").optional().parse() is MISSING
assert z.string().optional().default("guest").parse() == "guest"
assert z.object({"tags": z.string().array().default_factory(list)}).parse({}) == {
    "tags": [],
}
```

## Fallbacks and Overwrites

```python
from zodish import z

score = z.integer().catch(0)          # validation failure or missing -> 0
score.parse("bad")                    # 0
score.parse(10)                       # 10

z.string().optional().nonoptional().safe_parse()  # Failure: required
z.string().overwrite(str.upper).parse("ab")       # "AB"
z.integer().validate(1)                           # True
z.string().optional().unwrap()                    # the original string schema
```

* `.catch(value)` returns a deep-copied fallback for validation failures and
  missing input. A zero-argument callable is invoked instead of copied.
  Exceptions raised by callbacks still propagate.
* `.nonoptional()` rejects missing input, overriding an inner optional/default.
* `.overwrite(fn)` transforms a parsed value without changing its static type.
* `.validate(value)` returns a boolean; callback exceptions still propagate.
* `.unwrap()` returns the tracked inner schema for wrappers and container
  elements/values, and raises `TypeError` when no inner schema is tracked.

## Objects and Collections

```python
from zodish import z

user = z.object({"name": z.string(), "age": z.integer()}).strict()
user.parse({"name": "Alex", "age": 30})
# {"name": "Alex", "age": 30}
user.safe_parse({"name": "Alex", "age": 30, "admin": True}).success  # False
```

Objects accept Python dictionaries with string keys. Extra keys are stripped by default; `.strict()` rejects them and `.passthrough()` preserves them. Arrays accept lists; set schemas accept Python sets and return new sets; map schemas use dictionaries with a validated key schema and value schema. Tuple schemas accept lists or tuples and return tuples. Records accept dictionaries with string keys and validate every value.

```python
z.array(z.integer()).nonempty()
z.set(z.integer())
z.map(z.string(), z.integer())  # dictionary with validated keys and values
z.tuple(z.string(), z.integer()).rest(z.boolean())
z.record(z.string())
z.record(z.integer(), z.string())  # keyed record
```

`.required()` makes every object field reject missing input, including fields with `.optional()` or defaults. `.partial(*keys)` makes all fields optional, or only the selected keys. `.keyof()` returns an enum schema of the shape keys. `.length(n)` on strings and arrays requires exactly `n` characters or items.

`.catchall(schema)` validates unknown object keys and preserves them in the output; `.strict()`, `.strip()`, and `.passthrough()` clear it. Records accept an optional key schema: `z.record(z.integer(), z.string())` validates keys before values and uses parsed keys in the output. Tuples support a trailing rest schema: `z.tuple(z.string()).rest(z.integer())`.

No automatic coercion occurs. Booleans are not numbers. `z.number()` accepts exact Python `int` values and finite exact `float` values; `z.integer()` accepts exact `int` values. Numeric subclasses are rejected. The `.int()` constraint on a number also permits integral floats, retaining their type. Literals compare both type and value, so `True`, `1`, and `1.0` are distinct. Integer diagnostics use bit counts when Python's decimal conversion limit prevents formatting. String operations use the built-in `str` methods, including for string subclasses.

Integer comparisons retain arbitrary precision, including for bounds and literals:

```python
from zodish import z

huge = 10**5000
assert z.integer().min(huge).parse(huge + 1) == huge + 1
assert z.literal(huge).parse(huge) == huge
assert not z.literal(huge).safe_parse(huge + 1).success
```

Parsed object/list/record containers are newly constructed. Preserved extra fields and constant defaults are deep-copied. `unknown()`, `instance_of()`, and custom callbacks may return existing references; there is no universal deep-copy guarantee.

## Choices, Unions, and Intersections

### Literals and enums

```python
z.literal("admin")
z.enum(["user", "admin"])
z.union(z.string(), z.integer())
z.string().or_(z.integer())
```

Unions validate each branch in order and return the first success; a failure includes every branch's issues in `branches`. Every failing branch is evaluated before failure is reported, so deeply nested non-discriminated unions can grow exponentially with nesting depth. Use `z.discriminated_union()` when branches share a literal discriminator. `z.enum()` reports a single `invalid_value` issue.

### Discriminated unions

`z.discriminated_union(key, options)` dispatches on a dictionary key. Mapping keys are the expected discriminator values; the selected branch validates the whole dictionary.

```python
event = z.discriminated_union("kind", {
    "created": z.object({"kind": z.literal("created"), "id": z.integer()}),
    "deleted": z.object({
        "kind": z.literal("deleted"), "id": z.integer(), "reason": z.string(),
    }),
})

event.parse({"kind": "created", "id": 1})
```

A missing discriminator reports `required` at `(key,)`; an unknown value reports one `invalid_value` issue with `values` params. The selected branch controls the output, so include the discriminator in each branch shape to preserve it.

### Intersections

`z.intersection(left, right)` or `left.and_(right)` validates the same input against both schemas and merges dictionary outputs:

```python
both = z.intersection(
    z.object({"name": z.string()}),
    z.object({"age": z.integer()}),
)

both.parse({"name": "Alex", "age": 30})
# {"name": "Alex", "age": 30}
```

Both schemas must produce dictionaries; otherwise validation reports `invalid_intersection_types`. Nested dictionaries merge recursively; conflicting non-dictionary values take the right side. Missing input is rejected, so wrap the intersection in `.optional()` when absence is allowed. Strict object components still reject each other’s keys, just as they do at runtime.

## Comparisons and String Predicates

```python
z.integer().gt(0).lt(100)       # exclusive bounds
z.integer().gte(0).lte(100)     # inclusive aliases for min/max
z.number().multiple_of(0.25)
z.string().starts_with("v").ends_with(".json").includes("config")
```

Exclusive bound failures use `too_small`/`too_big` with `inclusive=False`; multiple-of failures use `not_multiple_of` with the `divisor` in `params`; and string predicate failures use `invalid_format`. Multiple checks between integral values are exact, including integral floats. Other float checks use remainders with a bounded roundoff tolerance, so `0.3` counts as a multiple of `0.1` despite binary floating-point representation. Divisors must be positive finite numbers.

## Formats and Python Scalars

Common string formats are syntax checks:

```python
z.string().url()
z.string().uuid()
z.string().ulid()
z.string().cuid()
z.string().cuid2()
z.string().nanoid()
z.string().jwt()
z.string().e164()
z.string().base64()
z.string().base64url()
z.string().date()
z.string().time()
z.string().datetime()
z.string().ipv4()
z.string().ipv6()
z.string().hostname()
```

Python-native value schemas validate object types rather than strings:

```python
z.date()        # datetime.date, excluding datetime.datetime
z.datetime()    # datetime.datetime
z.time()        # datetime.time
z.timedelta()   # datetime.timedelta
z.decimal()     # decimal.Decimal
z.bytes()       # exact bytes
z.path()        # pathlib.PurePath and subclasses
z.json()        # recursively JSON-compatible Python values
z.native_enum(Color)  # returns Color members
```

`z.json()` accepts `None`, booleans, integers, finite floats, strings, lists, tuples, and dictionaries with string keys. Cycles raise `RecursionError`. `z.native_enum()` returns enum members, not raw values. JSON Schema exports `z.json()` as `{}` and native enums as their JSON-compatible member values; the other Python object schemas are not representable.

## Coercion and stringbool

```python
z.coerce.string()   # str, int, or finite float -> str
z.coerce.number()   # int, finite float, or numeric string -> int | float
z.coerce.integer()  # int, integral float, or integer string -> int
z.coerce.boolean()  # bool, 0/1, or true/false-like string -> bool

z.string().stringbool()
z.string().trim().stringbool()
```

Coercion is explicit and Python-specific:

- Strings accept `str`, exact `int`, and finite exact `float`; booleans and containers are rejected.
- Numbers accept exact `int`/finite `float` and strings parsed with `int` first, then `float`; booleans and non-finite results are rejected.
- Integers accept `int`, integral floats, and integer strings; `"3.5"` is rejected.
- Booleans accept `bool`, `0`/`1`, and the case-insensitive strings `true`, `1`, `yes`, `on`, `false`, `0`, `no`, and `off`.
- Failed conversions report `invalid_type` instead of raising programming errors.
- Static annotations still describe the validated type; coercion widens runtime inputs beyond what the type checker sees.

`z.string().stringbool()` normalizes case by default and returns a Python `bool`. `truthy`, `falsy`, `case_sensitive`, and `message` are configurable; use `.trim()` first when surrounding whitespace is acceptable. JSON Schema exports coercion as its output type. `stringbool` exports as `boolean` in output mode and as a string enum in input mode; case-insensitive variants are not enumerated.

## Codecs

A codec validates and converts between an encoded and a decoded schema in both
directions:

```python
from datetime import datetime
from zodish import z

iso_datetime = z.codec(
    z.string().datetime(),
    z.datetime(),
    decode=lambda text: datetime.fromisoformat(text),
    encode=lambda value: value.isoformat(),
)

iso_datetime.parse("2026-10-20T12:30:00")   # datetime(...)
iso_datetime.decode("2026-10-20T12:30:00")  # same as parse
iso_datetime.encode(datetime(2026, 10, 20, 12, 30))
# "2026-10-20T12:30:00"
```

Use `z.codec_async()` when either conversion awaits I/O, then call
`.decode_async()`/`.encode_async()` (or their safe variants):

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

Async codecs preserve `.input_schema`, `.output_schema`, optional/nullable
wrappers, records, arrays, unions, and tuples. Dataclass codecs support async
field codecs through `.decode_async()`/`.encode_async()`.

Built-in helpers cover common string boundaries:

```python
z.codecs.iso_date()       # "2026-10-20" <-> date
z.codecs.iso_time()       # "12:30:00" <-> time
z.codecs.iso_datetime()   # ISO datetime <-> datetime
z.codecs.decimal()        # decimal string <-> Decimal
z.codecs.json_text()      # JSON text <-> JSON-compatible values
z.codecs.path()           # path string <-> pathlib.Path
z.codecs.bytes_base64()   # padded base64 <-> bytes
z.codecs.bytes_base64(urlsafe=True)
```

Each returns a `CodecSchema`. Invalid JSON reports `invalid_format` with a
`position`; base64 and decimal syntax failures report the corresponding format.

Structured domain objects use an explicit field map:

```python
z.codecs.dataclass(User, fields={
    "name": z.string(),
    "created": z.codecs.iso_datetime(),
})                        # encoded dict <-> dataclass instance
```

`z.codecs.dataclass()` infers field schemas from type annotations; pass
`fields=` to override selected fields. Supported annotations include primitives,
`bytes`, date/time/datetime/timedelta, `Decimal`, `Path`, `Enum`, `Literal`,
`list`/`Sequence`, `dict`/`Mapping` with string keys, fixed tuples, unions and
`Optional`, `Any`/`object`, and `Annotated`. `Annotated[T, schema]` supplies an
inline field schema or codec, with `fields=` taking precedence. Non-schema
metadata is ignored; multiple schema entries are rejected. PEP 695 type aliases
are resolved, including generic aliases with ordinary type parameters and
defaults, nested containers, and unions. Recursive aliases and variadic type
parameters require explicit field schemas. See the
[dataclass codec guide](docs/how-to/use-codecs.md) for examples.
Inference describes Python values; override date/time, `Decimal`,
`Path`, or enum fields with codecs when the encoded form is a string or raw
value. Plain schemas act as validators; codec overrides supply encoded and
decoded forms. Nested dataclasses and collections or unions containing them are
inferred recursively; recursive dataclass cycles are rejected. `InitVar` is
unsupported. `init=False` fields are ignored. Fields with dataclass defaults
may be omitted from input, and the dataclass default wins over a field-schema
default. A field schema that permits missing input without a dataclass default
is rejected. Input-mode JSON Schema describes the encoded object; output mode
describes the decoded object, so Python-only field values remain unrepresentable.

For direct construction, decorate the class with `@z.dataclass`:

```python
from typing import Annotated
from zodish import z

@z.dataclass
class User:
    name: Annotated[str, z.string().trim().min(1)]
    age: Annotated[int, z.integer().min(0)]
    nickname: str | None = None

User(name=" Alex ", age=30)  # normalized and validated
```

Constructor arguments are decoded values; encoded input still uses
`z.codecs.dataclass(User)`. Defaults and factories are validated once. The
decorator supports ordinary dataclasses with `init=True`, frozen/slots/keyword-only
options, `InitVar` and `init=False` fields, a `__post_init__` matching the
InitVar parameters, and one or more dataclass bases (ordinary or validated)
plus non-dataclass mixins. Standard dataclass MRO field-order and
duplicate-field rules apply. Custom `__init__`/`__new__`/`__setattr__`,
dataclass-level `init=False`, variadic generic classes/bases, undecorated
subclasses, `transform`/`coerce`/`stringbool`/`pipe` field schemas, and async
field schemas without `async_fields=True` are rejected. Use `__post_init__` for
post-validation normalization, classmethod factories for alternate signatures,
field codecs for conversions, and `z.create_model` for runtime fields. Generic classes bind
unbound type parameters to `Any`; a decorated parameterized subclass such as
`class IntBox(Box[int])` substitutes concrete type arguments into inherited
fields. `Box[int](...)` itself cannot see the runtime type argument.
Self-referential validated classes such as trees support direct construction
with nested instances, synchronous and async codec `parse`/`encode` with nested
encoded dictionaries, and JSON Schema export using `$ref`/definitions. InitVar
values are validated and passed to `__post_init__` but
not stored or encoded; defaulted `init=False` fields are validated before
post-init. Use `.overwrite()` for type-preserving normalization or a codec for
type-changing conversions.
`dataclasses.replace()` revalidates; by default assignment, `copy`, and `pickle`
bypass validation. Pass `validate_assignment=True` to validate declared-field
assignments and post-init writes with the same field schemas; the option is
inherited by decorated subclasses, requires a non-frozen dataclass, and leaves
the previous value in place when validation fails. Assignment values are decoded
values, like constructor arguments. `copy` and `pickle` remain outside that
contract. Decorated classes also expose class-level boundary methods:
`User.parse(...)`, `parse_json`, `safe_parse`, `validate`, their async variants,
`encode`, `safe_encode`, async encode, and `to_json_schema`. They delegate to a
cached `z.codecs.dataclass(User)`. Inherit `z.ValidatedModel` to expose these
methods to type checkers; every decorated class receives them at runtime unless
a user-defined method or field name already occupies the name. For fields
discovered at runtime, `z.create_model("User", fields, defaults=...)` creates a
keyword-only validated dataclass with the same codecs, classmethods, JSON
Schema, and assignment options; field attributes are statically `Any`. Async
field schemas are opt-in with `@z.dataclass(async_fields=True)` and require
`await Model.acreate(...)` for decoded-value construction; direct construction
raises when async construction schemas are present. See the
[validated construction guide](docs/how-to/use-codecs.md#validate-direct-construction).

Codec combinators preserve both directions:

```python
z.codecs.array(codec)      # list[encoded] <-> list[decoded]
codec.record()             # dict[str, encoded] <-> dict[str, decoded]
codec.optional()           # missing or encoded <-> missing or decoded
codec.nullable()           # None or encoded <-> None or decoded
codec.nonoptional()
codec.nullish()
z.codecs.union(codec_or_schema, ...)
z.codecs.tuple(codec_or_schema, ...)
```

`z.codecs.union()` and `z.codecs.tuple()` accept codecs or plain validator
schemas; plain schemas act as identity branches. Item errors carry list indices,
dictionary keys, or tuple positions, and aggregate across siblings.

- Decoding validates encoded input, runs `decode`, then validates the result
  through the output schema. Encoding validates decoded input, runs `encode`,
  then validates the result through the input schema.
- `.parse()` and `.decode()` are equivalent. `.safe_decode()` and
  `.safe_encode()` return `Success`/`Failure` for expected validation failures;
  programming errors from callbacks propagate.
- Codecs compose in objects, arrays, tuples, records, unions, discriminated
  unions, intersections, and optional/nullable/nonoptional wrappers. `.encode()`
  walks a reversible schema tree, so container encoding is available when every
  component is reversible.
- Reversible components are primitives, validators/constraints, codecs,
  optional/nullable/nonoptional wrappers, and structural containers. One-way
  operations — transforms, overwrites, refinements, coercions and `stringbool`,
  defaults and `default_factory`, catches, pipes, preprocessors, and lazy
  schemas — have no reverse path; `.encode()` raises `TypeError` when they are
  present.
- Use validator schemas for both sides of a codec. One-way transforms inside
  the input or output schema are not supported for encoding.
- `z.codec` callbacks are synchronous; use `z.codec_async` and
  `.decode_async()`/`.encode_async()` for awaitable conversions.
- Dataclass codecs support async field codecs through
  `.decode_async()`/`.encode_async()`; use the synchronous methods only when
  every reached field is synchronous.
- JSON Schema export uses the input schema for `io="input"` and the output
  schema for `io="output"`. A decoded side containing Python-only values such as
  `datetime` is not representable.

## Refinements and Transforms

```python
from dataclasses import dataclass
from zodish import z

@dataclass(frozen=True)
class User:
    name: str
    age: int

user_schema = z.object({
    "name": z.string().min(1),
    "age": z.integer().nonnegative(),
}).transform(lambda data: User(**data))

user: User = user_schema.parse({"name": "Alex", "age": 30})

positive_length = z.string().transform(len).pipe(z.integer().positive())
ordered = z.object({"start": z.integer(), "end": z.integer()}).refine(
    lambda data: data["start"] <= data["end"], "start must not exceed end",
)
```

Refinement paths are relative to the current location:

```python
from zodish import Issue, z

credentials = z.object({
    "password": z.string(),
    "confirm": z.string(),
}).super_refine(lambda value: Issue(
    "custom", "Passwords must match", ("confirm",),
) if value["password"] != value["confirm"] else None)
```

`Schema[Out, In]` tracks parsed output and accepted input types for primitives, homogeneous collections (including sets and maps), transforms, preprocessors, and wrappers. `In` defaults to `Out`, so `Schema[T]` remains valid when input and output types match. `default()` and `default_factory()` accept input-typed values; after a transform, pass the pre-transform type or apply the default before the transform.

Python type checkers cannot generally derive a new `TypedDict` from a dictionary of schema instances as TypeScript does with `z.infer`. Use `z.typed_object(TypedDictClass, shape)` when a statically typed dictionary result is needed; plain objects return `dict[str, Any]`. `z.tuple()` infers fixed-length tuple types for up to four items, while larger tuples and tuples with a rest schema return `tuple[Any, ...]`. Use `z.codecs.dataclass(Cls, fields=...)` or a transform to produce a statically typed dataclass result; `@z.dataclass` adds typed constructor arguments and attributes through `dataclass_transform`. Unions infer up to four alternatives; larger unions fall back to `Any`.

A `.brand(NewType)` marker changes only the static output type and is a runtime no-op. There is no type-checker plugin or generated model API.

`safe_parse` catches only `ValidationError`. Exceptions raised by callback code such as `int("bad")` propagate. Use `.refine()` to reject expected invalid values before a transform; use `z.preprocess` for intentional input conversion. Callbacks can raise `ValidationError` to contribute issues to the surrounding validation. Supplied issue paths are preserved as written. A callback raising `ValidationError([])` produces a `custom` issue at the current path with the message `Invalid value`, so the rejection still aggregates with sibling failures. Sync callbacks are used by default. Asynchronous checks are available through `refine_async`/`super_refine_async`/`transform_async`/`overwrite_async`/`default_factory_async`/`catch_async`/`z.preprocess_async` and `parse_async`/`safe_parse_async`/`validate_async`/`parse_json_async`/`safe_parse_json_async`, including inside objects, arrays, records, tuples, unions, intersections, pipes, and wrappers; async callbacks passed to synchronous builders are rejected. `z.codec_async` supports awaitable codec callbacks and `encode_async`/`safe_encode_async` support async reverse graphs, including async dataclass codecs. Lazy factories remain synchronous; a factory may return a schema containing async operations. See [Async support](docs/explanation/async-support.md) for details and limitations.

## Recursion

```python
from typing import Any
from zodish import Schema, z

node: Schema[dict[str, Any]] = z.lazy(lambda: z.object({
    "name": z.string(),
    "children": node.array().default([]),
}))

node.parse({"name": "root", "children": [{"name": "leaf"}]})
```

Inputs must be acyclic. The native evaluator raises `RecursionError` at 256 nested schema-node evaluations, including wrappers, to bound Rust stack use. It also raises `RecursionError` when the remaining thread stack is too small, so a callback that re-enters `parse` on a small-stack thread fails cleanly instead of exhausting the native stack. A lazy node resolves its factory once per parse and reuses that schema for the rest of the parse; a later parse invokes the factory again. Python callbacks may additionally hit Python's recursion limit. Containers are shallow-snapshotted before traversal so callbacks can mutate the original list or dictionary without invalidating the iteration. Email validation checks basic syntax only; regexes use Python's `re.search` behavior. This core implements async composition, callback families, JSON parsing, async codecs, async dataclass codecs, and async encoding, but it does not implement async lazy factories, JSON Schema import, or all Zod formats. See [Async support](docs/explanation/async-support.md).

## Limits and security notes

- `.regex()` evaluates author-supplied Python patterns with `re.search`. A pathological pattern can backtrack catastrophically on crafted input; never use untrusted patterns. The built-in format helpers use fixed patterns.
- Nested non-discriminated unions evaluate every failing branch, so worst-case cost grows exponentially with nesting. Use `z.discriminated_union()` when branches share a discriminator.
- On GIL builds, native evaluation holds the GIL for the duration of a synchronous parse. Very large inputs can block other Python threads; there is no GIL-release path. Free-threaded wheels do not use a GIL, but evaluation still runs on the calling thread.

## Metadata

Attach documentation metadata to any schema:

```python
from zodish import z

user = z.object({
    "name": z.string().min(1).meta({"title": "Name", "examples": ["Alex"]}),
}).meta({"title": "User", "description": "A registered user"})

user.metadata["title"]  # "User"
user.describe("Updated description").metadata["description"]
```

`metadata` is a read-only mapping. `.meta()` merges keys and overrides existing ones; `.describe()` replaces `description`. Metadata follows value-preserving derivations such as constraints, refinements, transforms, optional/default wrappers, and object structural methods. Collection and composition constructors such as `.array()`, `.or_()`, `z.union()`, `z.record()`, and `z.intersection()`/`.and_()` start with empty metadata. Metadata does not affect validation, equality, or hashing.

Container and composition schemas retain their child schemas: `.item` on arrays, `.value_schema` on records, `.items` on tuples, `.options` on unions, and `.options` (a mapping) on discriminated unions. This keeps nested metadata available to tooling such as a future JSON Schema exporter.

## JSON Schema Export

Export the supported subset with `to_json_schema()` or `schema.to_json_schema()`:

```python
from zodish import to_json_schema, z

schema = z.object({
    "name": z.string().min(1).meta({"title": "Name"}),
    "age": z.integer().min(18),
})

to_json_schema(schema)
# {"type": "object", "properties": {...}, "required": [...],
#  "additionalProperties": False}

to_json_schema(schema, target="openapi-3.0", io="input")
```

- `target` is `"draft-2020-12"` (default) or `"openapi-3.0"`.
- `io` is `"output"` (default) or `"input"`; it affects required fields, defaults, and the object extra-key policy.
- Metadata `title`, `description`, `examples`, and other keys are merged into the output and override generated keys. For OpenAPI 3.0, `examples` is emitted as the singular `example`. Metadata values are copied as-is; use JSON-compatible values when serializing the result.
- Supported: strings with length/regex/format and starts/ends/includes constraints, numbers/integers with inclusive or exclusive bounds and multiple-of checks, booleans, null, unknown, JSON values, native enums, literals, enums, arrays, tuples with optional rest (draft only), objects with catchall, records, unions, discriminated unions, intersections with dictionary outputs, optional/nullable/default wrappers, coercion wrappers, stringbool, and codecs. Static defaults must be JSON-compatible.
- Multiple string predicate patterns and multiple `multiple_of` constraints are emitted through `allOf`; multiple raw regex constraints on one string remain unrepresentable.
- Recursive schemas export as references when each lazy node declares an `id` in metadata: `z.lazy(factory).meta({"id": "Node"})` becomes `{"$ref": "#/$defs/Node"}` with the definition under `$defs` (OpenAPI uses `components/schemas`). A lazy schema without an id remains unrepresentable. The exporter resolves each lazy factory once per export.
- Not representable: `catch`, `refine`/`super_refine`, `transform`, `overwrite`, `preprocess`, `pipe`, `default_factory`, `instance_of`, `trim`/`lower`/`upper`, and regexes with non-default flags. Export raises `UnrepresentableError` by default; pass `unrepresentable="any"` to emit `{}` for those nodes instead.
- Tuples, keyed records, and null-only schemas are not representable in OpenAPI 3.0. Set schemas export as arrays with `uniqueItems: true`; their JSON output is a list, and `parse_json` still requires Python set input. Map schemas export as keyed records. Use `.nullable()` for nullable fields; standalone null literals and enum values containing `None` are rejected.
- Keyed records in draft JSON Schema require keys that produce strings. Integer, transformed, or otherwise non-string key schemas are rejected; one-argument records keep the default string-key behavior.
- String formats are JSON Schema annotations; validators enforce them only when format assertion is enabled. Python regex patterns are emitted as `pattern`, so Python-specific syntax may not be portable.
- JSON Schema `multipleOf` uses exact numeric semantics. Runtime integer checks are exact, but float checks apply a small quotient tolerance, so results can differ for decimal values such as `0.3` with divisor `0.1`.
- Python object schemas such as `z.date()`, `z.datetime()`, `z.time()`, `z.timedelta()`, `z.decimal()`, `z.bytes()`, `z.path()`, and `z.instance_of()` are not representable in JSON Schema. Native enum values must themselves be JSON-compatible.
- Output mode uses `additionalProperties: false` for stripping objects; input mode omits it, matching which input keys are accepted.
- `optional` is represented through object `required` lists; it has no standalone JSON Schema equivalent. `unknown` becomes `{}` and can describe only JSON values.
- JSON Schema cannot preserve Python's exact numeric-type distinctions, including `True`/`1`/`1.0` literals and int versus integral float. Runtime validation still enforces them.
- Discriminated unions export as `oneOf`; include each discriminator literal in its branch for precise matching.
- Intersections export as `allOf`. Strip-object components omit `additionalProperties` inside the intersection so combined inputs validate. Intersections whose statically known outputs are not dictionaries are unrepresentable.
- Coercion exports as its output type; broader accepted runtime inputs are not represented. `stringbool` input mode enumerates the configured strings but not all case variants.
- Codecs export the input schema for `io="input"` and the output schema for `io="output"`. The selected side may itself be unrepresentable, for example a decoded `datetime`.

## Architecture

The Rust core starts at `rust/lib.rs`, which declares modules and registers the Python extension. `rust/schema/evaluate.rs` dispatches synchronous validation, with container evaluation in `rust/schema/evaluate/containers.rs`; `rust/session.rs` and its child modules handle async sessions. Python schema builders construct native nodes; `parse()` enters the Rust evaluator once. Rust handles schema traversal, primitive validation, collection construction, bounds, optional/default handling, unions, and issue aggregation. There is no Python validation fallback. User callbacks, Python regular expressions, string case/whitespace operations, and `deepcopy` use Python operations to preserve their existing behavior. The public result/error classes and static typing remain Python code.

## Performance

Built in Rust for speed. Typical benchmarks (Apple M4 Max, Python 3.14.3, `parse` unless noted):

| Scenario | Operations/sec | Time per operation |
|---|---:|---:|
| `z.string().min(3).max(32)` | 7,475,470 | 0.13 µs |
| Object parse | 2,642,532 | 0.38 µs |
| Object failure (`safe_parse`) | 977,485 | 1.02 µs |
| Object failure, issues inspected | 437,031 | 2.29 µs |
| 1,000-element integer array | 173,938 | 5.75 µs |
| Union, first branch | 4,802,304 | 0.21 µs |
| Union, last branch | 1,457,000 | 0.69 µs |
| Recursive tree (depth 4) | 110,799 | 9.03 µs |
| Refinement callback | 7,066,718 | 0.14 µs |

Numbers are medians over seven repetitions from `benchmarks/bench_parse.py`. The array row uses the native fast path for primitive item validators and exact-list iteration, the recursive row benefits from per-parse lazy resolution, object parsing skips the unknown-key scan and input copy when every input key belongs to the shape, and failures defer Python `Issue` materialization and message formatting until they are read. Results depend on the host, Python version, and native build profile; the harness has no pass/fail thresholds. A pinned cross-library comparison with Pydantic, msgspec, and Marshmallow is recorded under [`benchmarks/results/`](benchmarks/results/), along with a release-wheel [async benchmark summary](benchmarks/results/2026-10-21-macos-arm64-async-release.md).

## Development

With the virtual environment activated, install the package in editable mode with the development tools, then run the checks from the repository root:

```sh
python -m pip install --group dev -e .
python -m unittest discover -s tests -v
python examples/quickstart.py
python benchmarks/bench_parse.py --smoke
python benchmarks/bench_async.py --smoke
ruff check .
mypy
cargo fmt --check
cargo clippy --locked -- -D warnings
cargo deny check
```

Editable installation picks up Python edits. After changing any source under `rust/`, rebuild the native extension with `python -m pip install -e .` before running tests; an editable install does not automatically rebuild Rust code.

If `just` is installed, run `just` to list the recipes. Use `PYTHON=python just install` to install dependencies, `just check` for static checks, `PYTHON=python just ci` to run static checks, tests, and examples, or `just bench` for synchronous benchmarks or `just bench-async` for the async harness. `PYTHON` selects the interpreter for recipes that run Python; its default is `python3.14`.

Ruff and strict mypy settings live in `pyproject.toml`; the `dev` dependency group installs both tools. Python 3.14+ syntax is intentional. `py.typed` is included for type checkers. `Cargo.lock` pins the Rust dependencies. The [`deny.toml`](deny.toml) policy fails on security advisories, yanked releases, disallowed licenses, duplicate crate versions, wildcard requirements, and non-crates.io sources; install `cargo-deny` with `cargo install cargo-deny --locked` to run it locally. The [CI workflow](.github/workflows/tests.yml) runs tests, the quickstart, and synchronous/async benchmark smoke runs on Ubuntu with Python 3.14 and 3.15, plus free-threaded 3.14t and 3.15t source builds, and runs the same `cargo deny` dependency checks. Its packaging job builds the abi3 wheel and sdist on Ubuntu, macOS, and Windows with Python 3.14, verifies both artifact contents, then installs each artifact and runs the test suite, quickstart, and native-extension check against it; it also re-runs the abi3 wheel smoke test under Python 3.15 on Linux. CI also runs a non-publishing free-threaded packaging preflight for cp314t/cp315t on every supported platform; the publish workflow builds and smoke-tests those wheels for Linux x86_64/aarch64 glibc, macOS x86_64/arm64, and Windows x64. The [typing examples](examples/typing_checks.py) are checked by mypy.

### Benchmarks

The benchmark harness measures successful and failing object parses, a failure whose issues are inspected, a 1,000-element array, union branch position, a recursive tree, and a Python refinement callback:

```sh
python benchmarks/bench_parse.py
python benchmarks/bench_parse.py --json
python benchmarks/bench_parse.py --filter union
```

It reports median throughput over repeated runs and has no pass/fail thresholds: results depend on the host, Python version, and native build profile. The failing scenarios use `safe_parse`, and one of them reads `.issues`; the other scenarios use `parse`. `--smoke` uses minimal counts for CI.

The async harness measures synchronous graphs through `parse_async`, immediately completing callbacks, `asyncio.sleep(0)` suspension, sequential callbacks, nested failures, and concurrent parses. Event loops are created outside timed iterations, and `--memory` records retained and peak `tracemalloc` values:

```sh
python benchmarks/bench_async.py
python benchmarks/bench_async.py --json
python benchmarks/bench_async.py --filter concurrent --memory
```

For comparisons with Pydantic, msgspec, and Marshmallow, use the optional pyperf
harness:

```sh
python -m pip install --group benchmark
python benchmarks/bench_compare.py --check-only
python benchmarks/bench_compare.py --build-profile release -o comparison.json
python benchmarks/summarize.py comparison.json --output comparison.md
```

Use `--build-profile release` only with a verified release build. See the
[benchmark guide](benchmarks/README.md) for installation, scenario coverage,
semantic differences, and result interpretation.

### Build distributions

Install maturin into the environment to make its CLI available, then build a release wheel and source distribution:

```sh
python -m pip install 'maturin>=1.15,<2'
maturin build --release --locked --interpreter python --out dist
maturin sdist --out dist
python scripts/verify_dist.py dist
```

Artifacts are written to `dist/`. `just dist` runs the two maturin build commands when maturin is already installed, and `just dist-check` also verifies the wheel layout, stable ABI tag, sdist contents, and version agreement. In a clean environment, install an artifact and run `python examples/verify_native.py` to confirm that the compiled extension, rather than a source fallback, is loaded.

## Stability & Versioning

Zodish follows [Semantic Versioning](https://semver.org/). Until 1.0, minor releases may include breaking changes; they are recorded in [CHANGELOG.md](CHANGELOG.md).

Covered by SemVer guarantees:

- The `z` schema builders, schema methods, and `parse`/`safe_parse`/`validate`/`parse_json`/`safe_parse_json`/`validate_json`/`parse_async`/`safe_parse_async`/`validate_async`/`parse_json_async`/`safe_parse_json_async`/`encode`/`safe_encode`/`encode_async`/`safe_encode_async` behavior.
- The public result and error classes: `ValidationError`, `Issue`, `Success`, `Failure`, and `MISSING`.
- `Schema[Out, In]` typing and the `py.typed` marker.
- JSON Schema export behavior for the supported subset.

Not covered:

- Rust internals (`rust/`), benchmarks, and CI configuration.
- Error message wording and performance numbers, which may change within a minor release.

## Author

Aleksandr Pavlov <ckidoz@gmail.com>

## License

MIT — see [LICENSE](LICENSE) for details.
