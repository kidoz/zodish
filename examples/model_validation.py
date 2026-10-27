from typing import Annotated

from zodish import Issue, ValidationError, z


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


registration = Registration.parse({
    "name": "Alex",
    "password": "secret",
    "confirmation": "secret",
})
assert registration.name == "Alex"
assert Registration.encode(registration) == {
    "displayName": "Alex",
    "password": "secret",
    "confirmation": "secret",
}

tenant_name = z.string().refine_with_context(
    lambda value, tenant: value == tenant,
    "Name does not belong to this tenant",
)
assert tenant_name.parse("acme", context="acme") == "acme"
assert not tenant_name.safe_parse("acme", context="other").success

print("Model validators, aliases, and validation context passed")
