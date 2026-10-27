"""Check an email against an async store while validating the other fields.

Run with: python examples/async_signup.py

``refine_async`` runs after the email schema succeeds. ``parse_async`` still
reports a sibling field failure from the same call. Synchronous ``parse``
raises ``TypeError`` for a schema that reaches an async check.
"""

import asyncio

from zodish import Failure, z


_TAKEN = {"taken@example.com"}


async def email_is_available(email: str) -> bool:
    await asyncio.sleep(0)
    return email not in _TAKEN


signup = z.object({
    "email": z.string().trim().lower().email().refine_async(
        email_is_available,
        "Email is already registered",
    ),
    "name": z.string().trim().min(1),
})


async def demo() -> None:
    accepted = await signup.parse_async({
        "email": " Ada@Example.com ",
        "name": " Ada ",
    })
    assert accepted == {"email": "ada@example.com", "name": "Ada"}

    rejected = await signup.safe_parse_async({
        "email": "taken@example.com",
        "name": " ",
    })
    assert isinstance(rejected, Failure)
    assert [(issue.path, issue.code) for issue in rejected.error.issues] == [
        (("email",), "custom"),
        (("name",), "too_small"),
    ]
    assert rejected.error.issues[0].message == "Email is already registered"

    malformed = await signup.safe_parse_async({
        "email": "not-an-email",
        "name": "Ada",
    })
    assert isinstance(malformed, Failure)
    assert malformed.error.issues[0].path == ("email",)
    assert malformed.error.issues[0].code == "invalid_format"
    assert malformed.error.issues[0].message != "Email is already registered"

    try:
        signup.parse({"email": "ada@example.com", "name": "Ada"})
    except TypeError:
        pass
    else:
        raise AssertionError("parse should reject an async schema")

    print(accepted)
    print(rejected.error.prettify())


if __name__ == "__main__":
    asyncio.run(demo())
