# How to handle errors

Use this guide when you need to return validation failures from an API, display
them in a form, or debug a rejected value.

## Decide between `parse` and `safe_parse`

- `schema.parse(value)` returns the parsed value or raises `ValidationError`.
  Use it when invalid input is a programming error.
- `schema.safe_parse(value)` returns `Success[T] | Failure`. Use it when invalid
  input is expected, such as request bodies.

```python
result = schema.safe_parse(value)
if result.success:
    handle(result.data)
else:
    handle_issues(result.error.issues)
```

`Success` and `Failure` form a discriminated union on `success`, so check it
before reading `data` or `error`.

## Read the issue fields

Every issue has:

| Field | Meaning |
| --- | --- |
| `code` | Machine-readable category, such as `too_small` or `invalid_type` |
| `message` | Human-readable text |
| `path` | Tuple of object keys and collection indices |
| `params` | Structured details for the failure |

```python
schema = z.object({"users": z.object({"age": z.integer().min(18)}).array()})
result = schema.safe_parse({"users": [{"age": 12}]})

issue = result.error.issues[0]
issue.path     # ("users", 0, "age")
issue.code     # "too_small"
issue.params   # {"minimum": 18, "inclusive": True, "origin": "number"}
```

Paths include array indices, so a nested failure identifies the exact element.

## Serialize failures for an API

`to_dict()` returns a JSON-serializable dictionary. Each issue's params are
merged into the serialized issue:

```python
import json

json.dumps(result.error.to_dict())
```

```json
{
  "issues": [
    {
      "minimum": 18,
      "inclusive": true,
      "origin": "number",
      "code": "too_small",
      "message": "Must be at least 18",
      "path": ["users", 0, "age"]
    }
  ]
}
```

## Shape failures for a form

Three helpers present the same issues in different shapes:

```python
error.flatten()   # {"form_errors": [...], "field_errors": {...}}
error.treeify()   # nested {"errors": [...], "properties": {...}, "items": [...]}
error.prettify()  # newline-separated human-readable lines
```

Use `flatten()` for flat forms, `treeify()` for nested field UIs, and
`prettify()` for logs or CLI output.

```python
try:
    schema.parse({"users": [{"age": "twelve"}]})
except ValidationError as error:
    print(error.flatten())
    print(error.treeify())
    print(error.prettify())
```

## Inspect union failures

A union reports one `invalid_union` issue. Each branch's issues are available in
the issue's `branches` attribute, so you can show which alternative came closest
or explain every accepted shape.

```python
result = z.union(z.string(), z.integer()).safe_parse([])
issue = result.error.issues[0]
issue.code       # "invalid_union"
issue.branches   # one tuple of issues per branch
```

`z.enum()` is different: an invalid value produces one `invalid_value` issue, not
a union failure.

## Add issues from callbacks

A refinement callback can return issues, and any callback can raise
`ValidationError` to contribute issues to the surrounding validation:

```python
raise ValidationError([Issue("custom", "Passwords must match", ("confirm",))])
```

Raised issue paths are preserved as written and are relative to the current
location. Raising `ValidationError([])` produces a `custom` issue at the current
path with the message `Invalid value`, so the rejection still aggregates with
sibling failures. See [Transform and refine values](transform-and-refine.md).

## Know what `safe_parse` catches

`safe_parse` catches only `ValidationError`. Exceptions from callback code, such
as `int("bad")` inside a transform, propagate unchanged. Validate expected
invalid values with `.refine()` before transforming when you want them reported
as issues.

## Related

- [Errors reference](../reference/errors.md)
- [Transform and refine values](transform-and-refine.md)
- [API reference](../reference/api.md)
