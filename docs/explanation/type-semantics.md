# Type semantics and static typing

Zodish validates Python values with explicit, Python-specific rules. This page
explains those rules and how they interact with static typing.

## Explicit over implicit

There is no automatic coercion. A string is not a number, and a boolean is not an
integer:

```python
z.integer().safe_parse("30").success   # False
z.number().safe_parse(True).success    # False
```

This keeps runtime validation predictable and makes the accepted input explicit
in the schema. When string or numeric input should be converted, use
[coercion](#explicit-coercion).

## Numeric rules

- `z.number()` accepts exact Python `int` values and finite exact `float` values.
- `z.integer()` accepts exact `int` values.
- Numeric subclasses are rejected.
- Booleans are not numbers, even though `bool` subclasses `int`.
- `.int()` on a number also permits integral floats and retains their type.
- Integer comparisons retain arbitrary precision, including bounds and literals.

```python
from zodish import z

huge = 10**5000
assert z.integer().min(huge).parse(huge + 1) == huge + 1
assert z.literal(huge).parse(huge) == huge
assert not z.literal(huge).safe_parse(huge + 1).success
```

Literals compare both type and value, so `True`, `1`, and `1.0` are distinct.

Float `multiple_of` checks compare the quotient to the nearest integer with a
small relative tolerance, so `0.3` counts as a multiple of `0.1` despite binary
floating-point representation. Integer multiple checks are exact. Divisors must
be positive finite numbers. JSON Schema `multipleOf` uses exact semantics, so
export and runtime can differ for decimals.

String operations use the built-in `str` methods, including for string
subclasses.

## Python object schemas

These schemas validate Python objects rather than strings:

| Schema | Accepts |
| --- | --- |
| `z.date()` | `datetime.date`, excluding `datetime.datetime` |
| `z.datetime()` | `datetime.datetime` |
| `z.time()` | `datetime.time` |
| `z.timedelta()` | `datetime.timedelta` |
| `z.decimal()` | `decimal.Decimal` |
| `z.bytes()` | Exact `bytes` |
| `z.path()` | `pathlib.PurePath` and subclasses |
| `z.json()` | Recursively JSON-compatible Python values |
| `z.native_enum(EnumClass)` | Returns enum members, not raw values |
| `z.instance_of(cls)` | Instances of `cls` |

`z.json()` accepts `None`, booleans, integers, finite floats, strings, lists,
tuples, and dictionaries with string keys. Cycles raise `RecursionError`.

Do not confuse these with the string format methods `.date()`, `.time()`, and
`.datetime()` on `z.string()`.

## Explicit coercion

Coercion is opt-in and Python-specific:

| Builder | Accepted input | Output |
| --- | --- | --- |
| `z.coerce.string()` | `str`, exact `int`, finite exact `float` | `str` |
| `z.coerce.number()` | Exact `int`, finite `float`, numeric string | `int` or `float` |
| `z.coerce.integer()` | `int`, integral float, integer string | `int` |
| `z.coerce.boolean()` | `bool`, `0`/`1`, true/false-like strings | `bool` |

Numbers parse with `int` first, then `float`. `"3.5"` is rejected by
`z.coerce.integer()`. Booleans accept `true`, `1`, `yes`, `on`, `false`, `0`,
`no`, and `off`, case-insensitively. Failed conversions report `invalid_type`
instead of raising programming errors.

`z.string().stringbool()` normalizes case by default and returns a Python `bool`.
Configure `truthy`, `falsy`, `case_sensitive`, and `message`; use `.trim()` first
when surrounding whitespace is acceptable.

Static annotations describe the validated output type. Coercion widens the
runtime inputs beyond what the type checker sees.

## Static typing

`Schema[Out, In]` tracks parsed output and accepted input types for primitives,
homogeneous collections, transforms, preprocessors, and wrappers. `In` defaults
to `Out`, so `Schema[T]` remains valid when input and output types match.

`default()` and `default_factory()` accept input-typed values. After a transform,
pass the pre-transform type or apply the default before the transform.

Python type checkers cannot generally derive a new `TypedDict` from a dictionary
of schema instances the way TypeScript infers types from Zod. As a result:

- objects return `dict[str, Any]`;
- `z.tuple()` infers fixed tuple types up to four items; larger tuples and
  tuples with a rest schema return `tuple[Any, ...]`;
- unions infer up to four alternatives, then fall back to `Any`;
- there is no type-checker plugin or generated model API.

Use a dataclass codec for a statically typed result that also encodes back:

```python
from dataclasses import dataclass
from zodish import z

@dataclass(frozen=True)
class User:
    name: str
    age: int

user_codec = z.codecs.dataclass(User, fields={
    "name": z.string(),
    "age": z.integer(),
})

user: User = user_codec.parse({"name": "Alex", "age": 30})
encoded = user_codec.encode(user)
```

A one-way transform can also produce a dataclass, but it has no reverse path:

```python
user_schema = z.object({
    "name": z.string(),
    "age": z.integer(),
}).transform(lambda data: User(**data))

user: User = user_schema.parse({"name": "Alex", "age": 30})
```

Schemas are immutable and their method chains preserve metadata, but the static
type generally widens when a refinement, transform, pipe, or wrapper is applied.

## Callbacks and errors

Synchronous callbacks are the default. Async callbacks passed to synchronous
builders raise `TypeError`; `refine_async`, `super_refine_async`,
`transform_async`, `overwrite_async`, `default_factory_async`, `catch_async`,
and `z.preprocess_async` use `parse_async`/`safe_parse_async` and may appear
anywhere in a schema. `z.codec_async` and asynchronous encoding use
`decode_async`/`encode_async` and their safe variants.
`safe_parse` and `safe_parse_async` catch only `ValidationError`; other callback
exceptions propagate. This keeps programming errors visible while still allowing
callbacks to report validation issues explicitly. See
[Async support](async-support.md) for the supported subset and limitations.

## Related

- [API reference](../reference/api.md)
- [JSON Schema reference](../reference/json-schema.md)
- [Transform and refine values](../how-to/transform-and-refine.md)
