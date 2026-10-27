"""Construct a validated model and convert it at the JSON boundary.

Run with: python examples/account.py

``Account(...)`` takes decoded values. ``Account.parse`` accepts wire names and
codecs, and ``Account.encode`` reverses that mapping. A dataclass default is a
stored value, so ``encode`` includes ``nickname`` as ``null``. Input-mode JSON
Schema describes the wire object. Output mode cannot describe the decoded
``datetime``.
"""

import json
from datetime import datetime
from typing import Annotated

from zodish import UnrepresentableError, ValidatedModel, ValidationError, z


@z.dataclass(frozen=True)
class Account(ValidatedModel):
    name: Annotated[str, z.string().min(1).wire("displayName")]
    email: Annotated[str, z.string().email()]
    created_at: Annotated[datetime, z.codecs.iso_datetime().wire("createdAt")]
    nickname: str | None = None


def main() -> None:
    created_at = datetime(2026, 10, 21, 12, 0)
    account = Account(
        name="Ada",
        email="ada@example.com",
        created_at=created_at,
    )
    assert account.nickname is None
    assert account.created_at == created_at

    try:
        Account(name="", email="ada@example.com", created_at=created_at)
    except ValidationError as error:
        assert error.issues[0].path == ("name",)
        assert error.issues[0].code == "too_small"
    else:
        raise AssertionError("blank name should be rejected")

    try:
        Account(
            name="Ada",
            email="ada@example.com",
            created_at="2026-10-21T12:00:00",  # type: ignore[arg-type]
        )
    except ValidationError as error:
        assert error.issues[0].path == ("created_at",)
    else:
        raise AssertionError("constructor should reject an encoded datetime")

    parsed = Account.parse({
        "displayName": "Ada",
        "email": "ada@example.com",
        "createdAt": "2026-10-21T12:00:00",
    })
    assert parsed == account
    encoded = Account.encode(parsed)
    assert encoded == {
        "displayName": "Ada",
        "email": "ada@example.com",
        "createdAt": "2026-10-21T12:00:00",
        "nickname": None,
    }
    assert Account.parse(encoded) == account

    openapi = Account.to_json_schema(target="openapi-3.0", io="input")
    assert set(openapi["properties"]) == {
        "displayName", "email", "createdAt", "nickname",
    }
    assert openapi["properties"]["createdAt"]["format"] == "date-time"
    assert openapi["properties"]["email"]["format"] == "email"
    try:
        Account.to_json_schema(io="output")
    except UnrepresentableError as error:
        assert "representable" in str(error)
    else:
        raise AssertionError("decoded datetime should not export")

    print(account)
    print(json.dumps(encoded))


if __name__ == "__main__":
    main()
