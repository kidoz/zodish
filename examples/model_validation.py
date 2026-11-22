"""Validate a registration with wire aliases and a cross-field password check.

Run with: python examples/model_validation.py
"""

from typing import Annotated

from zodish import Failure, Issue, ValidationError, z


@z.dataclass
class Registration(z.ValidatedModel):
    name: Annotated[str, z.string().wire_names(("displayName", "name"))]
    password: str
    confirmation: str

    @z.model_validator
    def passwords_match(self) -> None:
        if self.password != self.confirmation:
            raise ValidationError([
                Issue("custom", "Passwords must match", ("confirmation",)),
            ])


def main() -> None:
    passwords = {"password": "secret", "confirmation": "secret"}
    registration = Registration.parse({"displayName": "Alex", **passwords})
    assert registration.name == "Alex"
    assert Registration.parse({"name": "Alex", **passwords}) == registration
    assert Registration.encode(registration) == {"displayName": "Alex", **passwords}

    mismatch = Registration.safe_parse({
        "displayName": "Alex", "password": "secret", "confirmation": "different",
    })
    assert isinstance(mismatch, Failure)
    assert [(issue.path, issue.code) for issue in mismatch.error.issues] == [
        (("confirmation",), "custom"),
    ]

    tenant_name = z.string().refine_with_context(
        lambda value, tenant: value == tenant,
        "Name does not belong to this tenant",
    )
    assert tenant_name.parse("acme", context="acme") == "acme"
    assert not tenant_name.safe_parse("acme", context="other").success

    print("Model validators, aliases, and validation context passed")


if __name__ == "__main__":
    main()
