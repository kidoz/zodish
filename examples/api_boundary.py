"""Validate a JSON request and encode the response with wire names.

Run with: python examples/api_boundary.py

The request schema trims text and fills a default currency. A default has no
reverse path, and trim has no JSON Schema form, so ``encode`` and OpenAPI
export use a wire schema made of constraints, codecs, and wire names.

Both schemas are typed objects: parsing returns an ``Invoice`` TypedDict, so
static checkers see precise field types instead of ``dict[str, Any]``.
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, NotRequired, TypedDict, assert_type, cast

from zodish import Failure, Issue, z


class Line(TypedDict):
    sku: str
    quantity: int
    unit_price: Decimal


class Invoice(TypedDict):
    customer_name: str
    issued_at: datetime
    currency: str
    lines: list[Line]
    note: NotRequired[str]


quantity = z.integer().positive()
unit_price = z.codecs.decimal().wire("unitPrice")
issued_at = z.codecs.iso_datetime().wire("issuedAt")

invoice_request = z.typed_object(Invoice, {
    "customer_name": z.string().trim().min(1).wire("customerName"),
    "issued_at": issued_at,
    "currency": z.enum(["USD", "EUR"]).default("USD"),
    "lines": z.typed_object(Line, {
        "sku": z.string().trim().min(1),
        "quantity": quantity,
        "unit_price": unit_price,
    }).strict().array().min(1).wire("lineItems"),
    "note": z.string().trim().min(1).optional(),
}).strict()

invoice_wire = z.typed_object(Invoice, {
    "customer_name": z.string().min(1).wire("customerName"),
    "issued_at": issued_at,
    "currency": z.enum(["USD", "EUR"]),
    "lines": z.typed_object(Line, {
        "sku": z.string().min(1),
        "quantity": quantity,
        "unit_price": unit_price,
    }).strict().array().min(1).wire("lineItems"),
    "note": z.string().min(1).optional(),
}).strict().meta({"title": "Invoice"})


def client_message(issue: Issue) -> str:
    # These required fields report the TypedDict key. The extra key keeps
    # the name the client sent.
    if issue.path[:1] == ("issued_at",) and issue.code == "invalid_format":
        return "issuedAt must be an ISO-8601 datetime."
    if issue.code == "unrecognized_key" and issue.path:
        return f"Unknown field {issue.path[0]}."
    return issue.message


def main() -> None:
    body = """
    {
      "customerName": "  Northwind  ",
      "issuedAt": "2026-10-21T12:00:00",
      "lineItems": [
        {"sku": " PEN ", "quantity": 2, "unitPrice": "1.50"}
      ],
      "note": "  Net 30  "
    }
    """
    invoice = invoice_request.parse_json(body)
    assert_type(invoice, Invoice)
    assert invoice["customer_name"] == "Northwind"
    assert invoice["issued_at"] == datetime(2026, 10, 21, 12, 0)
    assert invoice["currency"] == "USD"
    assert invoice["lines"] == [{
        "sku": "PEN",
        "quantity": 2,
        "unit_price": Decimal("1.50"),
    }]
    assert invoice["note"] == "Net 30"

    # encode() is statically typed as Invoice (the schema's output type), but
    # the runtime result carries wire names, hence the cast.
    encoded = cast(dict[str, Any], invoice_wire.encode(invoice))
    assert encoded == {
        "customerName": "Northwind",
        "issuedAt": "2026-10-21T12:00:00",
        "currency": "USD",
        "lineItems": [{"sku": "PEN", "quantity": 2, "unitPrice": "1.50"}],
        "note": "Net 30",
    }
    assert invoice_wire.parse_json(json.dumps(encoded))["customer_name"] == "Northwind"

    openapi = invoice_wire.to_json_schema(target="openapi-3.0", io="input")
    assert openapi["title"] == "Invoice"
    assert set(openapi["properties"]) == {
        "customerName", "issuedAt", "currency", "lineItems", "note",
    }
    assert "note" not in openapi["required"]
    assert openapi["properties"]["issuedAt"]["format"] == "date-time"
    assert openapi["properties"]["lineItems"]["minItems"] == 1

    rejected = invoice_request.safe_parse_json(
        b"""
        {
          "customerName": " ",
          "issuedAt": "yesterday",
          "lineItems": [{"sku": "", "quantity": 0, "unitPrice": "nope"}],
          "extra": true
        }
        """,
        error=client_message,
    )
    assert isinstance(rejected, Failure)
    issues = {(issue.path, issue.code): issue.message for issue in rejected.error.issues}
    assert issues[("customer_name",), "too_small"]
    assert issues[("issued_at",), "invalid_format"] == (
        "issuedAt must be an ISO-8601 datetime."
    )
    assert issues[("lines", 0, "sku"), "too_small"]
    assert issues[("lines", 0, "quantity"), "too_small"]
    assert issues[("lines", 0, "unit_price"), "invalid_format"]
    assert issues[("extra",), "unrecognized_key"] == "Unknown field extra."
    assert rejected.error.flatten()["field_errors"]["lines"]

    print(json.dumps(encoded, indent=2))


if __name__ == "__main__":
    main()
