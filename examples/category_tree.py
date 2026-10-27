"""Validate a recursive category tree and export it as JSON Schema.

Run with: python examples/category_tree.py

The lazy schema needs an ``id`` so export can emit ``$ref``. A constant default
list is copied for each missing ``children`` field. Input mode leaves
``children`` optional. Output mode requires it, because every parsed object
contains it.
"""

from typing import Any

from zodish import Schema, z


category: Schema[dict[str, Any]] = z.lazy(lambda: z.object({
    "name": z.string().min(1),
    "children": category.array().default([]),
}).meta({"id": "Category"}))


def main() -> None:
    catalog = category.parse({
        "name": "Goods",
        "children": [{"name": "Pens", "children": [{"name": "Gel pens"}]}],
    })
    assert catalog == {
        "name": "Goods",
        "children": [{
            "name": "Pens",
            "children": [{"name": "Gel pens", "children": []}],
        }],
    }
    first = category.parse({"name": "A"})
    second = category.parse({"name": "B"})
    assert first["children"] == []
    assert first["children"] is not second["children"]

    exported = category.to_json_schema()
    assert exported["$ref"] == "#/$defs/Category"
    definition = exported["$defs"]["Category"]
    assert definition["properties"]["children"]["items"] == {"$ref": "#/$defs/Category"}
    assert definition["required"] == ["name", "children"]

    accepted = category.to_json_schema(io="input")
    assert accepted["$defs"]["Category"]["required"] == ["name"]

    print(catalog)
    print(exported["$ref"])


if __name__ == "__main__":
    main()
