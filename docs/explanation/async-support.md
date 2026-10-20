# Async support

Zodish has no asynchronous validation API. Every schema evaluation is
synchronous and runs in one native call. This page states exactly what that
means at the API boundary and why the library is built this way.

## Status

| Question | Answer |
| --- | --- |
| Is there an `await schema.parse(value)`? | No. `parse`, `safe_parse`, `validate`, `encode`, and `safe_encode` return directly. |
| Is there an async variant such as `parse_async` or `safe_parse_async`? | No. |
| Can `refine`, `super_refine`, `transform`, `overwrite`, `default_factory`, `catch`, `preprocess`, `lazy`, or codec callbacks be async? | No. Async callables are rejected. |
| Can `parse()` be called inside an async function? | Yes. It is an ordinary synchronous call and blocks the event loop while it runs. |
| Does Zodish manage event loops, tasks, or I/O? | No. |

## What is rejected, and when

Schema construction rejects coroutine functions and async generator functions
for every callback-taking API:

- `.refine()` and `.super_refine()`;
- `.transform()` and `.overwrite()`;
- `.default_factory()` and `.catch()`;
- `z.preprocess()` and `z.lazy()`;
- codec `decode` and `encode`.

The error is `TypeError("<role> must be synchronous")`.

A synchronous function can still return an awaitable or an async generator.
That cannot be detected at construction, so it is rejected during evaluation:

```python
from zodish import z

async def is_unique(value):
    return True

def returns_coroutine(value):
    return is_unique(value)

schema = z.string().refine(returns_coroutine)
schema.safe_parse("x")
# TypeError: refine callback returned an asynchronous result;
# async validation is not supported
```

The returned coroutine is closed before the error is raised. `safe_parse` and
`safe_encode` catch only `ValidationError`, so this `TypeError` propagates
instead of becoming a `Failure`. `validate()` propagates it as well.

## Why the evaluator is synchronous

A Zodish parse enters the Rust evaluator once and runs the schema tree to
completion. Supporting async callbacks would require the evaluator to suspend
at callbacks, an async result contract, and a separate async API surface. None
of those exist. Wrapping the synchronous parser in `await` would only hide the
blocking call; it would not make validation asynchronous.

The current design keeps one evaluation path, one result type, and predictable
operation ordering.

## What to do instead

Keep async work outside the schema:

1. Await the I/O or data you need before parsing.
2. Validate the now-available data synchronously.
3. Use a refinement over the fetched data when the rule depends on it.

```python
async def create_user(payload, repository):
    existing = await repository.emails()
    user = user_schema.refine(
        lambda data: data["email"] not in existing,
        "Email is already registered",
        path=("email",),
    ).parse(payload)
    return user
```

If validation itself must run off the event loop, call the synchronous API in a
worker thread, for example with `asyncio.to_thread(schema.parse, payload)`.
Zodish does not create threads or tasks itself.

## Related

- [Transform and refine values](../how-to/transform-and-refine.md)
- [Type semantics](type-semantics.md)
- [API reference](../reference/api.md)
