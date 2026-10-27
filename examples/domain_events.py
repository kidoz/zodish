"""Accept one webhook shape per event type.

Run with: python examples/domain_events.py

The discriminator selects a single branch. An unknown type is one
``invalid_value`` issue, and the selected branch reports its own field errors
with wire names.
"""

from decimal import Decimal

from zodish import Failure, z


order_event = z.discriminated_union("type", {
    "order.created": z.object({
        "type": z.literal("order.created"),
        "order_id": z.integer().positive().wire("orderId"),
        "total": z.codecs.decimal(),
    }).strict(),
    "order.refunded": z.object({
        "type": z.literal("order.refunded"),
        "order_id": z.integer().positive().wire("orderId"),
        "reason": z.string().min(1),
    }).strict(),
})


def main() -> None:
    created = order_event.parse({
        "type": "order.created",
        "orderId": 9,
        "total": "12.00",
    })
    assert created == {
        "type": "order.created",
        "order_id": 9,
        "total": Decimal("12.00"),
    }
    assert order_event.encode(created) == {
        "type": "order.created",
        "orderId": 9,
        "total": "12.00",
    }

    refunded = order_event.parse({
        "type": "order.refunded",
        "orderId": 9,
        "reason": "damaged",
    })
    assert refunded["reason"] == "damaged"
    assert order_event.encode(refunded)["orderId"] == 9

    unknown = order_event.safe_parse({"type": "order.shipped", "orderId": 9})
    assert isinstance(unknown, Failure)
    assert unknown.error.issues[0].code == "invalid_value"
    assert unknown.error.issues[0].path == ("type",)
    assert unknown.error.issues[0].params["values"] == (
        "order.created", "order.refunded",
    )

    missing = order_event.safe_parse({"orderId": 9})
    assert isinstance(missing, Failure)
    assert missing.error.issues[0].code == "required"
    assert missing.error.issues[0].path == ("type",)

    invalid = order_event.safe_parse({
        "type": "order.refunded",
        "orderId": 0,
        "reason": "",
    })
    assert isinstance(invalid, Failure)
    assert [(issue.path, issue.code) for issue in invalid.error.issues] == [
        (("orderId",), "too_small"),
        (("reason",), "too_small"),
    ]

    print(order_event.encode(created))
    print(order_event.encode(refunded))


if __name__ == "__main__":
    main()
