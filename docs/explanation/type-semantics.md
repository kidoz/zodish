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

### Typed object results

Pass an explicit `TypedDict` to `z.typed_object` when the output dictionary should
have a static shape:

```python
from typing import TypedDict
from zodish import z

class User(TypedDict):
    name: str
    age: int

user_schema = z.typed_object(User, {
    "name": z.string().min(1),
    "age": z.integer().gte(0),
})

user = user_schema.parse({"name": "Alex", "age": 30})
# static type: User
```

The schema shape must cover every required `TypedDict` key and may include
declared optional keys. Shape keys outside the `TypedDict` are rejected at
construction. Choose field schemas whose outputs match the declared value
types; Zodish checks key declarations and required output presence.

A required key must be present after its field schema runs. An optional schema
or transform that produces `MISSING` raises a `required` issue at that key.
Defaults and factories run first, so they can supply a required key when input
omits it. `None` follows the field schema's nullable rules. `NotRequired` and
`total=False` keys retain their field-schema behavior; include an optional
schema when an included field may be omitted. `Required` and `NotRequired`
qualifiers also work with postponed annotations.

Checks run natively and aggregate sibling issues in both parsing modes.
JSON Schema output marks required keys as required; input allows keys supplied
by defaults to be omitted. Field constraints, metadata, and codec input/output
schemas are preserved. `.strict()`, `.passthrough()`, `.strip()`, and
`.catchall()` retain the typed result and its required-key checks, while
shape-changing methods such as `.pick()`, `.omit()`, and `.partial()` return
the ordinary `ObjectSchema` type.

A type checker does not build that `TypedDict` from the shape. mypy 2.4 types
a dict whose schema classes differ as `dict[str, object]`: the keys are `str`,
and each field's output type is gone. `z.object(...).parse` is therefore
`dict[str, Any]`. No overload recovers the shape, and Zodish does not ship a
type-checker plugin.

`z.typed_object` reports the class you wrote. A dict literal with those keys
is accepted. A value annotated `dict[str, Any]` is rejected, so untyped JSON
objects need `parse_json` or a cast. The checker does not compare a field
schema with the annotation: `age: int` paired with `z.string()` still
type-checks. `parse` and `encode` are both annotated as the `TypedDict`.
`.wire()` accepts and emits the wire name at runtime, so those calls disagree
with the keys the checker expects. For a boundary whose keys differ, use
`@z.dataclass` and `ValidatedModel.parse`: the constructor and `parse` result
are the class, and both the encoded input and the encoded output are
`dict[str, Any]`.

`.transform(fn)` is typed as the function's return type.
`z.create_model(name, object_schema)` builds a validated class from a plain
strip `z.object` and is `type[Any]` to the checker. The runnable forms are in
[examples/typed_objects.py](../../examples/typed_objects.py).

### Brands

A `NewType` brand changes only the static output type:

```python
from typing import NewType
from zodish import z

UserId = NewType("UserId", int)
user_id_schema = z.integer().positive().brand(UserId)

user_id = user_id_schema.parse(3)
# static type: UserId
```

`.brand()` is a runtime no-op and is terminal: the returned `Schema[Brand, In]`
does not expose narrower schema methods. Use a frozen dataclass codec or a
runtime transform when the value itself must become immutable; `.readonly()` is
not implemented.

Use a dataclass codec for a statically typed result that also encodes back:

```python
from dataclasses import dataclass
from typing import Annotated
from zodish import z

type Age = Annotated[int, z.integer().min(0)]

@dataclass(frozen=True)
class User:
    name: str
    age: Age

user_codec = z.codecs.dataclass(User)

user: User = user_codec.parse({"name": "Alex", "age": 30})
encoded = user_codec.encode(user)
```

Static tools see `Age` as `int`; the dataclass codec reads its schema metadata
and enforces the non-negative constraint. Aliases can be reused in containers
and unions, including generic aliases such as `type Items[T] = list[T]` used as
`Items[Age]`. Ordinary type-parameter defaults are supported; parameters without
defaults require explicit arguments. Bounds and constraints remain static type
checks. Schema instances remain runtime values: use
`Annotated[T, schema]` rather than using a schema instance as a type annotation.
When metadata contains a codec, `T` describes its decoded output. Validation
occurs through the codec; an ordinary `User(...)` call does not validate.

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
- [Typed objects example](../../examples/typed_objects.py)
