# How to handle missing values and defaults

Use this guide when an input may be absent, `None`, or invalid, and you need to
choose what the schema accepts and what it returns.

Zodish distinguishes three cases:

- **missing** — the key or argument was not supplied; represented by `MISSING`;
- **`None`** — the value is explicitly `None`;
- **a value** — anything else.

`optional()` handles only the first, `nullable()` handles only the second, and
`nullish()` handles both.

```python
from zodish import MISSING, z

z.string().optional().parse() is MISSING   # True
z.string().nullable().parse(None)           # None
z.string().nullish().parse() is MISSING     # True
z.string().nullish().parse(None)            # None
```

## Choose the wrapper that matches the contract

| Wrapper | Missing input | `None` input | Output for missing |
| --- | --- | --- | --- |
| no wrapper | rejected (`required`) | rejected (`invalid_type`) | — |
| `.optional()` | accepted | rejected (`invalid_type`) | `MISSING` |
| `.nullable()` | rejected (`required`) | accepted | — |
| `.nullish()` | accepted | accepted | `MISSING` |

A field is required by default, including `z.unknown()`. Missing optional object
fields are omitted from the output rather than returned as `MISSING`.

## Provide a default for missing input

```python
z.string().default("guest").parse()             # "guest"
z.object({
    "tags": z.string().array().default_factory(list),
}).parse({})                                    # {"tags": []}
```

Defaults apply only to missing input. A default value is copied and then passed
through the schema, so constraints and transforms also apply to it:

```python
z.string().min(3).default("ab").safe_parse().success   # False
```

`default_factory` runs only for missing input and is called on each parse.

A default inside an outer `optional()` is skipped for missing input, because the
outer wrapper short-circuits first:

```python
z.string().default("guest").optional().parse() is MISSING   # True
z.string().optional().default("guest").parse()              # "guest"
```

This ordering differs from Zod 4, where defaults short-circuit before
constraints. In Zodish the default is validated; see
[Missing values and defaults](../explanation/missing-none-defaults.md).

## Fall back when input is missing or invalid

`.catch()` returns a fallback for both validation failures and missing input:

```python
score = z.integer().catch(0)
score.parse("bad")   # 0
score.parse(10)      # 10
```

The fallback is deep-copied. Pass a zero-argument callable instead of a value when
each parse needs a fresh value:

```python
z.string().array().catch(list)
```

Exceptions raised by the callable still propagate.

## Reject missing input again

`.nonoptional()` overrides an inner optional or default:

```python
z.string().optional().nonoptional().safe_parse().success   # False
```

Use it when a field is optional in one schema variant but required in another.

## Recover the inner schema

`.unwrap()` returns the tracked inner schema for wrappers and container
elements/values. It raises `TypeError` when no inner schema is tracked:

```python
inner = z.string().optional().unwrap()   # the original string schema
```

## Related

- [Missing values and defaults](../explanation/missing-none-defaults.md)
- [API reference](../reference/api.md)
- [Errors reference](../reference/errors.md)
