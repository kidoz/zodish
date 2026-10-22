# How to refine and transform values

Use this guide when a value must satisfy a rule that built-in constraints cannot
express, or when parsed data must be converted to another shape.

## Reject values with `refine`

```python
ordered = z.object({"start": z.integer(), "end": z.integer()}).refine(
    lambda data: data["start"] <= data["end"],
    "start must not exceed end",
)
```

`.refine()` accepts a keyword-only `message=` and a relative `path=`:

```python
z.object({
    "start": z.integer(),
    "end": z.integer(),
}).refine(
    lambda data: data["start"] <= data["end"],
    "end must not precede start",
    path=("end",),
)
```

A failed refinement adds a `custom` issue. The path is relative to the current
location, so nested refinements still report a useful path.

## Report several issues with `super_refine`

`super_refine` may return one `Issue`, a list or tuple of `Issue` objects, or
`None`. Returned issue paths are relative to the current location:

```python
from zodish import Issue, z

credentials = z.object({
    "password": z.string(),
    "confirm": z.string(),
}).super_refine(lambda value: Issue(
    "custom", "Passwords must match", ("confirm",),
) if value["password"] != value["confirm"] else None)
```

Use `super_refine` when one check should produce more than one issue or when you
need control over issue codes.

## Convert the output with `transform`

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
```

Transforms run in declaration order and change the static output type. A
transform returns a general `Schema[T]`, so apply type-specific methods before
it.

## Chain stages with `pipe`

```python
positive_length = z.string().transform(len).pipe(z.integer().positive())
```

The output of the left schema becomes the input of the right schema. Use `pipe`
when a later schema should validate a transformed value.

## Change a value without changing its type with `overwrite`

```python
z.string().overwrite(str.upper).parse("ab")   # "AB"
```

`.overwrite(fn)` is for normalization that does not change the static type.

## Convert between encoded and decoded values

Use `z.codec` when a value needs both an encoded and a decoded form. See
[Use codecs](use-codecs.md) for the bidirectional workflow, reversible
operations, and JSON Schema selection.

## Convert input before validation with `preprocess`

Use `z.preprocess(fn, schema)` for intentional input conversion, such as parsing
a string before validating the result:

```python
import json

z.preprocess(json.loads, z.object({"id": z.integer()}))
```

Prefer `.refine()` for rejecting expected invalid values. Preprocess is for
conversion that should happen before the schema sees the value.

## Order operations deliberately

Try operations in this order:

1. type-specific constraints such as `.trim()`, `.min()`, and `.email()`;
2. object structural edits such as `.extend()` and `.partial()`;
3. refinements;
4. transforms, pipes, and wrappers.

Structural edits on objects preserve refinements, but refinements return a
general `Schema[T]`, as do transforms, pipes, and optional/default wrappers.
Type-specific methods therefore must come before those wrappers.

## Handle callback errors

Synchronous callbacks are the default. Async callbacks passed to `refine`,
`transform`, or other synchronous builders raise `TypeError` as programming
errors, and codec `decode`/`encode` callbacks are synchronous.
`refine_async`, `super_refine_async`, `transform_async`, `overwrite_async`,
`default_factory_async`, `catch_async`, and `z.preprocess_async` provide
asynchronous paths and must be awaited with `parse_async`/`safe_parse_async`.
They may be nested in containers, unions, intersections, pipes, and wrappers.
See [Async support](../explanation/async-support.md) for composition and
limitations.

`safe_parse` catches only `ValidationError`. Exceptions from callback code, such
as `int("bad")`, propagate unchanged. To turn an expected failure into an issue,
raise `ValidationError` inside the callback:

```python
from zodish import Issue, ValidationError

def parse_age(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise ValidationError([Issue("custom", "Invalid age", ())]) from None
```

Raised issue paths are preserved as written and are relative to the current
location. Raising `ValidationError([])` produces a `custom` issue at the current
path with the message `Invalid value`, so the rejection still aggregates with
sibling failures.

## Related

- [Handle errors](handle-errors.md)
- [Type semantics](../explanation/type-semantics.md#static-typing)
- [API reference](../reference/api.md)
