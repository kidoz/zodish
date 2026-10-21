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
z.codecs.decimal()        # decimal string <-> Decimal
z.codecs.json_text()      # JSON text <-> JSON-compatible values
z.codecs.path()           # path string <-> pathlib.Path
z.codecs.bytes_base64()   # padded base64 <-> bytes
z.codecs.bytes_base64(urlsafe=True)
```

Each helper returns a `CodecSchema` and accepts an optional `message` where a
syntax error is possible. Invalid JSON reports `invalid_format` with a
`position`; decimal and base64 failures report their format name.

## Map objects to dataclasses

Use `z.codecs.dataclass()` when an encoded dictionary should become a dataclass
instance and back:

```python
from dataclasses import dataclass
from datetime import datetime
from zodish import z

@dataclass(frozen=True)
class User:
    name: str
    created: datetime

user_codec = z.codecs.dataclass(User, fields={
    "created": z.codecs.iso_datetime(),
})

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
