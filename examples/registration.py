"""Collect field errors, then check rules that span fields.

Run with: python examples/registration.py

Each field is validated first, so a blank email and a short password are
reported together. The cross-field check runs after every field succeeds and
can attach a separate issue to ``confirm`` and to ``ends_on``.
"""

from datetime import date

from zodish import Failure, Issue, z


def check_registration(value: dict[str, object]) -> list[Issue] | None:
    issues: list[Issue] = []
    if value["password"] != value["confirm"]:
        issues.append(Issue("custom", "Passwords must match", ("confirm",)))
    starts_on = value["starts_on"]
    ends_on = value["ends_on"]
    if isinstance(starts_on, date) and isinstance(ends_on, date) and ends_on < starts_on:
        issues.append(Issue(
            "custom", "End date must not precede the start date", ("ends_on",),
        ))
    return issues or None


registration = z.object({
    "email": z.string().trim().lower().email(),
    "password": z.string().min(8),
    "confirm": z.string(),
    "starts_on": z.codecs.iso_date(),
    "ends_on": z.codecs.iso_date(),
}).super_refine(check_registration)


def main() -> None:
    fields = registration.safe_parse({
        "email": "not-an-email",
        "password": "short",
        "confirm": "short",
        "starts_on": "2026-10-21",
        "ends_on": "2026-10-01",
    })
    assert isinstance(fields, Failure)
    assert [(issue.path, issue.code) for issue in fields.error.issues] == [
        (("email",), "invalid_format"),
        (("password",), "too_small"),
    ]
    assert "email" in fields.error.flatten()["field_errors"]
    assert "password" in fields.error.flatten()["field_errors"]

    crossed = registration.safe_parse({
        "email": " Ada@Example.com ",
        "password": "correct-horse",
        "confirm": "other-horse",
        "starts_on": "2026-10-21",
        "ends_on": "2026-10-01",
    })
    assert isinstance(crossed, Failure)
    assert [(issue.path, issue.message) for issue in crossed.error.issues] == [
        (("confirm",), "Passwords must match"),
        (("ends_on",), "End date must not precede the start date"),
    ]

    accepted = registration.parse({
        "email": " Ada@Example.com ",
        "password": "correct-horse",
        "confirm": "correct-horse",
        "starts_on": "2026-10-01",
        "ends_on": "2026-10-21",
    })
    assert accepted == {
        "email": "ada@example.com",
        "password": "correct-horse",
        "confirm": "correct-horse",
        "starts_on": date(2026, 10, 1),
        "ends_on": date(2026, 10, 21),
    }
    print(accepted)
    print(crossed.error.prettify())


if __name__ == "__main__":
    main()
