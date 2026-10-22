# How to convert values in both directions with codecs

Use this guide when a value has an encoded form at the boundary and a decoded
Python form inside your application, and you need to validate both directions.

A codec pairs an input schema with an output schema and a conversion callback for
each direction.

## Build a codec

```python
from datetime import datetime
from zodish import z

iso_datetime = z.codec(
    z.string().datetime(),
    z.datetime(),
    decode=lambda text: datetime.fromisoformat(text),
    encode=lambda value: value.isoformat(),
)

iso_datetime.parse("2026-10-20T12:30:00")    # datetime(2026, 10, 20, 12, 30)
iso_datetime.decode("2026-10-20T12:30:00")   # same as parse
iso_datetime.encode(datetime(2026, 10, 20, 12, 30))
# "2026-10-20T12:30:00"
```

Decoding validates the encoded input, calls `decode`, then validates the result
through the output schema. Encoding validates the decoded input, calls `encode`,
then validates the result through the input schema.

When either conversion awaits I/O, build an async codec and await the matching
method:

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

`.decode_async()` and `.safe_decode_async()` are available on codecs;
`.encode_async()` and `.safe_encode_async()` are available on all schemas.
`encode_async` walks the same reversible graph as `encode`, so plain reversible schemas, sync
codecs, async codecs, and containers all work. `.decode()` and `.encode()` on a
codec that reaches async callbacks raise `TypeError`.

## Use a built-in codec

The `z.codecs` namespace provides codecs for common string boundaries:

```python
z.codecs.iso_date()       # "2026-10-20" <-> date
z.codecs.iso_time()       # "12:30:00" <-> time
z.codecs.iso_datetime()   # ISO datetime <-> datetime
z.codecs.iso_duration()   # ISO 8601 duration <-> timedelta
z.codecs.decimal()        # decimal string <-> Decimal
z.codecs.json_text()      # JSON text <-> JSON-compatible values
z.codecs.path()           # path string <-> pathlib.Path
z.codecs.bytes_base64()   # padded base64 <-> bytes
z.codecs.bytes_base64(urlsafe=True)
```

Each helper returns a `CodecSchema` and accepts an optional `message` where a
syntax error is possible. Invalid JSON reports `invalid_format` with a
`position`; decimal and base64 failures report their format name.

`iso_duration()` accepts weeks (`P2W`), days and time components
(`P1DT2H30M`), fractional seconds, and an optional sign. Calendar years and
months are rejected because `timedelta` cannot represent them exactly. Encoding
uses a canonical form such as `PT15M` or `P1DT2H3M4.5S`.

## Map objects to dataclasses

Use `z.codecs.dataclass()` when an encoded dictionary should become a dataclass
instance and back:

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from zodish import z

type Name = Annotated[str, z.string().min(1)]

@dataclass(frozen=True)
class User:
    name: Name
    created: Annotated[datetime, z.codecs.iso_datetime()]

user_codec = z.codecs.dataclass(User)

user = user_codec.parse({
    "name": "Alex", "created": "2026-10-20T12:30:00Z",
})
user_codec.encode(user)
# {"name": "Alex", "created": "2026-10-20T12:30:00+00:00"}
```

Field schemas are inferred from type annotations; `fields=` overrides selected
entries. Supported annotations include primitives, `bytes`,
date/time/datetime/timedelta, `Decimal`, `Path`, `Enum`, `Literal`,
`list`/`Sequence`, `dict`/`Mapping` with string keys, fixed tuples, unions and
`Optional`, `Any`/`object`, and `Annotated`. Inference describes Python values;
override date/time, `Decimal`, `Path`, or enum fields with codecs when the
encoded form is a string or raw value. Plain schemas act as validators; codec
overrides supply both encoded and decoded forms. Nested dataclasses and
collections or unions containing them are inferred recursively; recursive
dataclass cycles are rejected. `InitVar` is unsupported, and `init=False` fields
are ignored. Fields with dataclass
defaults may be omitted from input, and the dataclass default wins over a
field-schema default. A field schema that permits missing input without a
dataclass default is rejected. Input-mode JSON Schema describes the encoded
object; output mode describes the decoded object, so Python-only field values
remain unrepresentable. Async field codecs are supported through
`.decode_async()`/`.encode_async()`; field issue paths and dataclass defaults are
preserved.

`Annotated[T, schema]` uses that schema as the complete field definition, just
like a `fields=` entry. A plain schema validates the field; a codec converts
between its encoded and decoded forms. `T` must describe the schema's decoded
output, including any transforms; this correspondence is the caller's
responsibility. `fields=` takes precedence over annotation metadata. Metadata
that is not a Zodish schema is ignored. Multiple schema entries, including
entries combined through nested annotations or aliases, raise `TypeError`;
combine constraints into one schema chain instead.

PEP 695 aliases (`type Name = ...`) and traditional assignment aliases work in
supported annotations, including container items, unions, and nested
dataclasses. Generic PEP 695 aliases accept ordinary type parameters and their
defaults:

```python
from dataclasses import dataclass
from typing import Annotated
from zodish import z

type Age = Annotated[int, z.integer().min(0)]
type Items[T] = list[T]
type Pair[T, U = T] = tuple[T, U]

@dataclass(frozen=True)
class Batch:
    values: Items[Age]
    nested: Items[Items[Age]]
    pair: Pair[Age]

batch_codec = z.codecs.dataclass(Batch)
batch = batch_codec.parse({"values": [1], "nested": [[2]], "pair": [3, 4]})
# Batch(values=[1], nested=[[2]], pair=(3, 4))
```

Type arguments retain their `Annotated` schemas and codecs. Defaults may
reference earlier parameters, as `U = T` does above, and all-default aliases may
be used without subscription. Parameters without defaults require explicit
arguments; missing or excess arguments raise `TypeError`. Type-parameter bounds
and constraints remain the type checker's responsibility; runtime validation
comes from the substituted annotations and schemas.

Recursive or unresolved aliases and variadic parameters (`TypeVarTuple` and
`ParamSpec`) raise `TypeError`; use `fields=` for an explicit schema in those
cases. Finite nesting such as `Items[Items[Age]]` is supported. This resolves
aliases; parameterized dataclass classes such as `Box[int]` remain unsupported.
An alias remains a Python type annotation. For an ordinary dataclass,
validation happens through the dataclass codec; calling `User(...)` directly
uses the ordinary constructor and does not validate its arguments. Use
[`@z.dataclass`](#validate-direct-construction) when construction itself should
validate.

## Validate direct construction

`z.codecs.dataclass()` validates encoded input and returns an instance, but for
an ordinary dataclass, `User(...)` remains the standard constructor. Decorate
the class with `@z.dataclass` when construction should validate and normalize
decoded field values:

```python
from typing import Annotated
from zodish import z


@z.dataclass
class User:
    name: Annotated[str, z.string().trim().min(1)]
    age: Annotated[int, z.integer().min(0)]
    nickname: str | None = None


user = User(name=" Alex ", age=30)   # name is "Alex"
```

Constructor arguments are **decoded values**, typed by the annotations. Encoded
input remains a boundary concern: use `z.codecs.dataclass(User).parse(...)` for
wire data, including codec fields whose encoded and decoded types differ. For
example, a field annotated `Annotated[datetime, z.codecs.iso_datetime()]`
accepts a `datetime` in the constructor and an ISO string through the codec.

Defaults and default factories are validated as constructor inputs. An invalid
default raises `ValidationError`; a factory runs once when its field is omitted
and is not called for supplied fields. Schema-level `.default()` and
`.optional()` wrappers do not make a constructor parameter optional; the
dataclass signature controls requiredness.

Explicit `Annotated[T, schema]` schemas must produce `T`. Clearly incompatible
pairs such as `Annotated[int, z.string()]` fail at decoration with `TypeError`.
Other explicit schemas receive a native output guard; a value that does not
match the declared annotation raises `TypeError` during construction. Inferred
schemas are trusted. Explicit overrides may use additional guard-supported
annotations, including sets, `TypedDict` classes, arbitrary classes, keyed
records with non-string keys, and homogeneous tuples.

`z.codecs.dataclass(User)` remains the parse, encode, and JSON Schema entry
point for a decorated class. Its decode path validates supplied fields once,
validates omitted defaults, checks explicit overrides against the class
annotations, and assembles the instance without calling the validating
initializer. This avoids duplicate transforms and `__post_init__` calls. An
ordinary stdlib dataclass keeps the existing codec behavior, including its
default and override semantics.

Error behavior:

| Situation | Exception |
| --- | --- |
| Missing required, extra, or bad constructor arguments | `TypeError` |
| Field or default validation failure | `ValidationError` with aggregated paths |
| Output/annotation mismatch | `TypeError` |
| Optional field schema without a dataclass default | `ValueError` |
| Async field schema in construction | `TypeError` |

The first slice supports ordinary dataclasses with `init=True`, defaults and
default factories, `frozen`, `slots`, `weakref_slot=True` with slots,
`kw_only`, and a zero-argument `__post_init__`. Custom `__init__`, `__new__`,
and `__setattr__`; `InitVar`; `init=False` fields; `preprocess`; `transform`,
`coerce`, `stringbool`, and `pipe` field schemas; and async construction are
rejected at decoration. Use `.overwrite()` for type-preserving normalization or
a codec for type-changing conversions. Lazy field schemas are also rejected
because their async capability is not statically known.

A validated class may inherit from one dataclass base (ordinary or validated)
plus non-dataclass mixins. Inherited fields, defaults, factories, overrides, and
`__post_init__` are validated with the subclass's annotations. Multiple
dataclass bases, generic or parameterized bases, a base with `init=False` or a
custom `__init__`, and inherited `InitVar`/`init=False` fields are rejected.
An undecorated subclass of a validated class is not validated; the codec rejects
it, and applying stdlib `@dataclasses.dataclass` to it does not restore
validation. Decorate the subclass with `@z.dataclass` to validate its fields.

`@z.dataclass` uses `dataclass_transform`, so type checkers see typed
constructor arguments and attributes. Literal `frozen=True` and `kw_only=True`
options are reflected statically; options computed at runtime are not. Apply
`@z.dataclass` as the outermost decorator. A bare `@z.dataclass` can wrap an
existing supported dataclass; applying options to an existing dataclass is
rejected. A plain outer `@dataclasses.dataclass` preserves the validating
initializer, but reverse stacking and option combinations remain unsupported.

`dataclasses.replace()` revalidates because it calls the initializer.
Assignment, `copy`, `pickle`, and direct `object.__new__` bypass validation.
Fields mutated by `__post_init__` are not revalidated.

## Compose codecs

Codec combinators preserve conversions in both directions:

```python
z.codecs.array(iso_datetime)   # list of encoded strings <-> list of datetimes
iso_datetime.record()          # dict values
iso_datetime.optional()        # missing or encoded <-> missing or decoded
iso_datetime.nullable()        # None or encoded <-> None or decoded
iso_datetime.nonoptional()
iso_datetime.nullish()
z.codecs.union(iso_datetime, z.string())
z.codecs.tuple(iso_datetime, z.string())
```

`z.codecs.union()` and `z.codecs.tuple()` accept codecs or plain validator
schemas. Plain schemas act as identity branches. Item failures carry list
indices, dictionary keys, or tuple positions and aggregate with sibling errors.

## Choose the right direction

| Call | Input | Output | Use when |
| --- | --- | --- | --- |
| `.parse(value)` / `.decode(value)` | Encoded | Decoded | Reading external data |
| `.safe_parse(value)` / `.safe_decode(value)` | Encoded | `Success`/`Failure` | Invalid input is expected |
| `.encode(value)` | Decoded | Encoded | Writing external data |
| `.safe_encode(value)` | Decoded | `Success`/`Failure` | Invalid decoded input is expected |

`safe_decode` is an alias for `safe_parse`. `safe_encode` returns the same
`Success[T] | Failure` union as `safe_parse`.

## Know when encoding is reversible

Encoding walks the schema tree and requires every component to have a reverse
path. Reversible components are:

- primitives and validators/constraints;
- codecs;
- optional, nullable, and nonoptional wrappers;
- structural containers: objects, arrays, tuples, records, unions,
  discriminated unions, and intersections.

One-way operations have no reverse path. `.encode()` raises `TypeError` when they
are present:

- transforms, refinements, and overwrites;
- coercions and `stringbool`;
- defaults, `default_factory`, and catches;
- pipes, preprocessors, and lazy schemas.

Use validator schemas on both sides of a codec. A one-way transform inside the
input or output schema prevents encoding.

```python
schema = z.object({"created": iso_datetime})
schema.encode({"created": datetime(2026, 10, 20, 12, 30)})
# {"created": "2026-10-20T12:30:00"}
```

## Export the correct side as JSON Schema

`io="input"` exports the codec's input schema; `io="output"` exports its output
schema:

```python
from zodish import to_json_schema

to_json_schema(iso_datetime, io="input")
# {"type": "string", "format": "date-time"}
```

A decoded side containing Python-only values such as `datetime` is not
representable, so export input mode when you need the wire format. Async codecs
export their input and output schemas the same way.

## Handle callback errors

`z.codec` callbacks are synchronous. Use `z.codec_async` and
`.decode_async()`/`.encode_async()` for awaitable conversions. Dataclass codecs
support async field codecs through the same async methods; synchronous
`.decode()`/`.encode()` reject reached async fields.
See [Async support](../explanation/async-support.md).
`safe_decode`/`safe_decode_async` and `safe_encode`/`safe_encode_async` catch
only `ValidationError`; programming errors from callbacks propagate.

## Related

- [Transform and refine values](transform-and-refine.md)
- [Export JSON Schema](export-json-schema.md)
- [API reference](../reference/api.md)
- [Errors reference](../reference/errors.md)
