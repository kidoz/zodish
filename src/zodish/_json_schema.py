"""JSON Schema export for Zodish schemas.

This module is imported lazily by ``zodish.schemas.to_json_schema`` so the
public schema module does not depend on the export layer at import time.
"""

from __future__ import annotations

import builtins
import math
import re
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from ._codecs import (
    AsyncCodecSchema,
    CodecSchema,
    _CompoundCodecSchema,
    _JsonTextSchema,
)
from ._native import NativeSchema
from .schemas import (
    _CONSTRAINT_KINDS,
    _KNOWN_FORMATS,
    _PATTERN_FORMATS,
    ArraySchema,
    DiscriminatedUnionSchema,
    IntersectionSchema,
    JSONSchema,
    JSONSchemaIO,
    JSONSchemaTarget,
    KeyedRecordSchema,
    MapSchema,
    ObjectSchema,
    Path,
    RecordSchema,
    Schema,
    SetSchema,
    TupleSchema,
    TypedObjectSchema,
    UnionSchema,
    UnrepresentableError,
    UnrepresentablePolicy,
    _field_wire,
)


def to_json_schema(
    schema: Schema[Any, Any],
    *,
    target: JSONSchemaTarget = "draft-2020-12",
    io: JSONSchemaIO = "output",
    unrepresentable: UnrepresentablePolicy = "throw",
) -> JSONSchema:
    """Export a schema as a JSON Schema mapping.

    Refinements, transforms, preprocessors, pipes, and factories are not
    representable. Lazy schemas are exported as references when they declare an
    ``id``; resolving them may run each lazy factory once per export.
    ``unrepresentable="any"`` emits ``{}`` for unsupported nodes.
    """
    if not isinstance(schema, Schema):
        raise TypeError("Expected a Schema")
    if target not in ("draft-2020-12", "openapi-3.0"):
        raise ValueError("Unknown JSON Schema target")
    if io not in ("input", "output"):
        raise ValueError("io must be 'input' or 'output'")
    if unrepresentable not in ("throw", "any"):
        raise ValueError("unrepresentable must be 'throw' or 'any'")
    context = _ExportContext()
    token = _EXPORT_CONTEXT.set(context)
    try:
        result = _export_schema(
            schema, (), target, io, unrepresentable, False,
        )
    finally:
        _EXPORT_CONTEXT.reset(token)
    if context.definitions:
        if target == "draft-2020-12":
            result = {**result, "$defs": context.definitions}
        else:
            result = {**result, "components": {"schemas": context.definitions}}
    return result


def _unsupported(
    path: Path, reason: str, unrepresentable: UnrepresentablePolicy,
) -> JSONSchema:
    if unrepresentable == "any":
        return {}
    raise UnrepresentableError(path, reason)


class _LazyCycleError(RuntimeError):
    """Raised when a lazy factory re-enters itself during export resolution."""


class _ExportContext:
    """Per-export lazy registry, definitions, and memoized factory results."""

    def __init__(self) -> None:
        self.definitions: dict[str, JSONSchema] = {}
        self.definition_nodes: dict[str, int] = {}
        self.resolved: dict[int, Schema[Any, Any]] = {}
        self.failures: dict[int, Exception] = {}
        self.resolving: builtins.set[int] = builtins.set()

    def resolve_lazy(self, node: NativeSchema) -> Schema[Any, Any]:
        key = id(node)
        resolved = self.resolved.get(key)
        if resolved is not None:
            return resolved
        failure = self.failures.get(key)
        if failure is not None:
            raise failure
        if key in self.resolving:
            raise _LazyCycleError
        self.resolving.add(key)
        try:
            result = node.args[0]()
        except Exception as error:
            self.failures[key] = error
            raise
        finally:
            self.resolving.discard(key)
        if not isinstance(result, Schema):
            raise TypeError("Lazy factory must return a Schema")
        self.resolved[key] = result
        return result

    def try_resolve_lazy(self, node: NativeSchema) -> Schema[Any, Any] | None:
        try:
            return self.resolve_lazy(node)
        except Exception:
            return None


_EXPORT_CONTEXT: ContextVar[_ExportContext | None] = ContextVar(
    "zodish_json_schema_export", default=None,
)


def _definition_ref(name: str, target: JSONSchemaTarget) -> str:
    escaped = name.replace("~", "~0").replace("/", "~1")
    if target == "draft-2020-12":
        return f"#/$defs/{escaped}"
    return f"#/components/schemas/{escaped}"


def _lazy_target(node: NativeSchema) -> Schema[Any, Any] | None:
    context = _EXPORT_CONTEXT.get()
    if context is None:
        return None
    target = context.try_resolve_lazy(node)
    if target is None or target._node.kind == "lazy":
        return None
    return target


def _export_lazy(
    schema: Schema[Any, Any],
    node: NativeSchema,
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
) -> JSONSchema:
    context = _EXPORT_CONTEXT.get()
    if context is None:
        raise RuntimeError("JSON Schema export context is not active")
    try:
        resolved = context.resolve_lazy(node)
    except _LazyCycleError:
        return _unsupported(
            path, "lazy schema cannot resolve itself", unrepresentable,
        )
    except Exception as error:
        raise UnrepresentableError(
            path, f"lazy factory failed: {error}",
        ) from error

    lazy_id = schema.metadata.get("id")
    resolved_id = resolved.metadata.get("id")
    if lazy_id is not None and resolved_id is not None and lazy_id != resolved_id:
        raise UnrepresentableError(
            path, "lazy and resolved schemas declare different ids",
        )
    name = lazy_id if lazy_id is not None else resolved_id
    if name is None:
        return _unsupported(
            path, "lazy schemas require an id for export", unrepresentable,
        )
    if not isinstance(name, str) or not name:
        raise UnrepresentableError(
            path, "lazy schema id must be a non-empty string",
        )

    identity = id(resolved._node)
    existing = context.definition_nodes.get(name)
    if existing is not None:
        if existing != identity:
            raise UnrepresentableError(
                path, f"duplicate schema id {name!r}",
            )
        return {"$ref": _definition_ref(name, target)}

    context.definition_nodes[name] = identity
    context.definitions[name] = {}
    if target == "draft-2020-12":
        definition_path: Path = (*path, "$defs", name)
    else:
        definition_path = (*path, "components", "schemas", name)
    context.definitions[name] = _export_schema(
        resolved,
        definition_path,
        target,
        io,
        unrepresentable,
        False,
    )
    return {"$ref": _definition_ref(name, target)}


def _inner_schema(schema: Schema[Any, Any], node: NativeSchema) -> Schema[Any, Any]:
    if schema._inner is not None:
        return schema._inner
    return Schema[Any, Any](node.children[0])


def _node_accepts_missing(node: NativeSchema, io: JSONSchemaIO) -> bool:
    kind = node.kind
    if kind == "lazy":
        target = _lazy_target(node)
        return False if target is None else _node_accepts_missing(
            target._node, io,
        )
    if kind == "wire":
        return _node_accepts_missing(node.children[0], io)
    if kind == "optional":
        return True
    if kind == "nonoptional":
        return False
    if kind == "required_output":
        return io == "input" and _node_missing_output(node.children[0]) is True
    if kind in ("catch", "async_catch"):
        if _node_accepts_missing(node.children[0], io):
            return True
        return io == "input"
    if kind in ("default", "default_factory", "async_default_factory"):
        return io == "input"
    if kind in _CONSTRAINT_KINDS or kind in ("nullable", "coerce", "string_bool"):
        return _node_accepts_missing(node.children[0], io)
    if kind == "union":
        return any(_node_accepts_missing(child, io) for child in node.children)
    if kind in (
        "pipe", "refine", "super_refine", "transform", "overwrite",
        "async_refine", "async_super_refine", "async_transform",
        "async_overwrite",
    ):
        return _node_accepts_missing(node.children[0], io)
    return False


def _node_missing_output(node: NativeSchema) -> bool | None:
    """Classify missing input for export: present, omitted, or rejected.

    This inspects schema structure only; lazy factories may be resolved during
    export, but other callbacks never run. Unsupported callback nodes retain
    the exporter's existing approximation.
    """
    kind = node.kind
    if kind == "lazy":
        target = _lazy_target(node)
        return None if target is None else _node_missing_output(target._node)
    if kind == "optional":
        return False
    if kind == "nonoptional":
        return None
    if kind in ("default", "default_factory", "async_default_factory"):
        return True
    if kind == "union":
        for child in node.children:
            result = _node_missing_output(child)
            if result is not None:
                return result
        return None
    if kind in ("catch", "async_catch"):
        result = _node_missing_output(node.children[0])
        return True if result is None else result
    if kind == "required_output":
        return True if _node_missing_output(node.children[0]) is True else None
    if kind == "pipe":
        result = _node_missing_output(node.children[0])
        return _node_missing_output(node.children[1]) if result is False else result
    if kind in _CONSTRAINT_KINDS or kind in (
        "wire", "nullable", "coerce", "string_bool", "refine", "super_refine",
        "transform", "overwrite", "async_refine", "async_super_refine",
        "async_transform", "async_overwrite",
    ):
        return _node_missing_output(node.children[0])
    return None


def _schema_accepts_missing(schema: Schema[Any, Any], io: JSONSchemaIO) -> bool:
    if isinstance(schema, AsyncCodecSchema):
        source = (
            schema.input_schema
            if io == "input"
            else schema._json_output_schema
        )
        return _node_accepts_missing(source._node, io)
    if isinstance(schema, _CompoundCodecSchema):
        source = (
            schema.input_schema
            if io == "input"
            else schema._json_output_schema
        )
        return _node_accepts_missing(source._node, io)
    if isinstance(schema, CodecSchema):
        source = schema.input_schema if io == "input" else schema.output_schema
        return _node_accepts_missing(source._node, io)
    return _node_accepts_missing(schema._node, io)


def _node_outputs_string(node: NativeSchema) -> bool:
    kind = node.kind
    if kind == "lazy":
        target = _lazy_target(node)
        return False if target is None else _node_outputs_string(target._node)
    if kind == "string":
        return True
    if kind == "literal":
        return isinstance(node.args[0], str)
    if kind == "enum":
        return all(isinstance(value, str) for value in node.args[0])
    if kind in _CONSTRAINT_KINDS or kind == "coerce":
        return _node_outputs_string(node.children[0])
    if kind == "union":
        return all(_node_outputs_string(child) for child in node.children)
    if kind in (
        "wire", "optional", "nonoptional", "required_output", "default",
        "default_factory",
    ):
        return _node_outputs_string(node.children[0])
    return False


def _node_outputs_dict(node: NativeSchema) -> bool:
    kind = node.kind
    if kind == "lazy":
        target = _lazy_target(node)
        return False if target is None else _node_outputs_dict(target._node)
    if kind in ("object", "record", "discriminated_union"):
        return True
    if kind == "intersection":
        return all(_node_outputs_dict(child) for child in node.children)
    if kind == "union":
        return all(_node_outputs_dict(child) for child in node.children)
    if kind in (
        "wire", "optional", "nonoptional", "required_output", "default",
        "default_factory", "coerce",
    ):
        return _node_outputs_dict(node.children[0])
    return False


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite float")
        return value
    if isinstance(value, (list, builtins.tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("Non-string mapping key")
        return {key: _json_value(item) for key, item in value.items()}
    raise ValueError("Unsupported value")


def _export_schema(
    schema: Schema[Any, Any],
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    result = _export_node(
        schema, path, target, io, unrepresentable, relax_strip,
    )
    if schema.metadata:
        metadata = dict(schema.metadata)
        metadata.pop("id", None)
        if target == "openapi-3.0" and "examples" in metadata:
            examples = metadata.pop("examples")
            if "example" not in metadata:
                if isinstance(examples, list):
                    if examples:
                        metadata["example"] = examples[0]
                else:
                    metadata["example"] = examples
        result = {**result, **metadata}
        if target == "openapi-3.0" and "$ref" in result and len(result) > 1:
            reference = result.pop("$ref")
            result = {"allOf": [{"$ref": reference}], **result}
    return result


def _export_node(
    schema: Schema[Any, Any],
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    if isinstance(schema, _JsonTextSchema):
        return {"type": "string"}
    if isinstance(schema, AsyncCodecSchema):
        source = (
            schema.input_schema
            if io == "input"
            else schema._json_output_schema
        )
        return _export_schema(
            source, path, target, io, unrepresentable, relax_strip,
        )
    if isinstance(schema, _CompoundCodecSchema):
        source = (
            schema.input_schema
            if io == "input"
            else schema._json_output_schema
        )
        return _export_schema(
            source, path, target, io, unrepresentable, relax_strip,
        )
    if isinstance(schema, CodecSchema):
        source = schema.input_schema if io == "input" else schema.output_schema
        return _export_schema(
            source, path, target, io, unrepresentable, relax_strip,
        )
    node = schema._node
    if node.kind == "lazy":
        return _export_lazy(
            schema, node, path, target, io, unrepresentable,
        )
    if node.kind in (
        "optional", "nonoptional", "required_output", "nullable", "default",
        "default_factory", "wire",
    ):
        return _export_wrapper(
            schema, node, path, target, io, unrepresentable, relax_strip,
        )
    constraints: list[NativeSchema] = []
    while True:
        if node.kind in _CONSTRAINT_KINDS:
            constraints.append(node)
            node = node.children[0]
            continue
        if node.kind == "coerce":
            node = node.children[0]
            continue
        break
    result = _export_base(
        schema, node, path, target, io, unrepresentable, relax_strip,
    )
    return _apply_constraints(
        result, constraints, node.kind, target, path, unrepresentable,
    )


def _export_wrapper(
    schema: Schema[Any, Any],
    node: NativeSchema,
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    kind = node.kind
    child = _inner_schema(schema, node)
    if kind in ("optional", "required_output", "wire"):
        return _export_schema(
            child, path, target, io, unrepresentable, relax_strip,
        )
    if kind == "nonoptional":
        result = dict(
            _export_schema(
                child, path, target, io, unrepresentable, relax_strip,
            ),
        )
        result.pop("default", None)
        return result
    if kind == "nullable":
        exported = _export_schema(
            child, path, target, io, unrepresentable, relax_strip,
        )
        if target == "openapi-3.0":
            return {**exported, "nullable": True}
        return {"anyOf": [exported, {"type": "null"}]}
    if kind == "default_factory":
        return _unsupported(
            path, "default_factory is not representable", unrepresentable,
        )
    try:
        default = _json_value(node.args[0])
    except ValueError:
        return _unsupported(
            path, "default value is not JSON-compatible", unrepresentable,
        )
    result = dict(
        _export_schema(
            child, path, target, io, unrepresentable, relax_strip,
        ),
    )
    result["default"] = default
    return result


def _export_base(
    schema: Schema[Any, Any],
    node: NativeSchema,
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    kind = node.kind
    if kind == "string":
        return {"type": "string"}
    if kind == "number":
        return {"type": "number"}
    if kind == "integer":
        return {"type": "integer"}
    if kind == "boolean":
        return {"type": "boolean"}
    if kind == "unknown":
        return {}
    if kind == "json":
        return {}
    if kind == "string_bool":
        if io == "input":
            truthy = list(node.args[0])
            falsy = list(node.args[1])
            return {"type": "string", "enum": truthy + falsy}
        return {"type": "boolean"}
    if kind == "exact_instance":
        return _unsupported(
            path,
            "Python-specific instance schemas are not representable",
            unrepresentable,
        )
    if kind == "native_enum":
        cls = node.args[0]
        try:
            values = [_json_value(member.value) for member in cls]
        except (TypeError, ValueError):
            return _unsupported(
                path,
                "native enum values are not JSON-compatible",
                unrepresentable,
            )
        if target == "openapi-3.0" and any(value is None for value in values):
            return _unsupported(
                path,
                "null enum values are not representable in OpenAPI 3.0",
                unrepresentable,
            )
        return {"enum": values}
    if kind == "none":
        if target == "openapi-3.0":
            return _unsupported(
                path,
                "null-only schemas are not representable in OpenAPI 3.0",
                unrepresentable,
            )
        return {"type": "null"}
    if kind == "literal":
        value = node.args[0]
        if target == "openapi-3.0":
            if value is None:
                return _unsupported(
                    path,
                    "null literals are not representable in OpenAPI 3.0",
                    unrepresentable,
                )
            return {"enum": [value]}
        return {"const": value}
    if kind == "enum":
        values = list(node.args[0])
        if target == "openapi-3.0" and any(value is None for value in values):
            return _unsupported(
                path,
                "null enum values are not representable in OpenAPI 3.0",
                unrepresentable,
            )
        return {"enum": values}
    if kind in ("array", "set"):
        item = (
            schema.item
            if isinstance(schema, (ArraySchema, SetSchema))
            else Schema[Any, Any](node.children[0])
        )
        result: JSONSchema = {
            "type": "array",
            "items": _export_schema(
                item,
                (*path, "items"),
                target,
                io,
                unrepresentable,
                relax_strip,
            ),
        }
        if kind == "set":
            result["uniqueItems"] = True
        return result
    if kind == "object":
        return _export_object(
            schema, node, path, target, io, unrepresentable, relax_strip,
        )
    if kind == "record":
        if isinstance(schema, (KeyedRecordSchema, MapSchema)):
            key_schema: Schema[Any, Any] | None = schema.key_schema
            value_schema = schema.value_schema
        elif isinstance(schema, RecordSchema):
            key_schema = None
            value_schema = schema.value_schema
        else:
            key_node = node.args[0]
            key_schema = (
                None if key_node is None else Schema[Any, Any](key_node)
            )
            value_schema = Schema[Any, Any](node.children[0])
        value_json = _export_schema(
            value_schema,
            (*path, "additionalProperties"),
            target,
            io,
            unrepresentable,
            relax_strip,
        )
        if key_schema is None:
            return {"type": "object", "additionalProperties": value_json}
        if target == "openapi-3.0":
            return _unsupported(
                path,
                "keyed records are not representable in OpenAPI 3.0",
                unrepresentable,
            )
        if not _node_outputs_string(key_schema._node):
            return _unsupported(
                path,
                "record keys must produce strings for JSON Schema",
                unrepresentable,
            )
        return {
            "type": "object",
            "propertyNames": _export_schema(
                key_schema,
                (*path, "propertyNames"),
                target,
                io,
                unrepresentable,
                relax_strip,
            ),
            "additionalProperties": value_json,
        }
    if kind == "tuple":
        return _export_tuple(
            schema, node, path, target, io, unrepresentable, relax_strip,
        )
    if kind == "intersection":
        if isinstance(schema, IntersectionSchema):
            left, right = schema.left, schema.right
        else:
            left = Schema[Any, Any](node.children[0])
            right = Schema[Any, Any](node.children[1])
        if not _node_outputs_dict(left._node) or not _node_outputs_dict(
            right._node,
        ):
            return _unsupported(
                path,
                "intersection outputs must be dictionaries",
                unrepresentable,
            )
        return {
            "allOf": [
                _export_schema(
                    left,
                    (*path, "allOf", 0),
                    target,
                    io,
                    unrepresentable,
                    True,
                ),
                _export_schema(
                    right,
                    (*path, "allOf", 1),
                    target,
                    io,
                    unrepresentable,
                    True,
                ),
            ],
        }
    if kind == "union":
        return _export_union(
            schema, node, path, target, io, unrepresentable, relax_strip,
        )
    if kind == "discriminated_union":
        return _export_discriminated_union(
            schema, node, path, target, io, unrepresentable, relax_strip,
        )
    return _unsupported(
        path, f"{kind} schemas are not representable", unrepresentable,
    )


def _export_object(
    schema: Schema[Any, Any],
    node: NativeSchema,
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    if isinstance(schema, (ObjectSchema, TypedObjectSchema)):
        shape = schema.shape
        policy = schema.unknown_keys
        catchall = schema._catchall
    else:
        shape = {
            key: Schema[Any, Any](child)
            for key, child in zip(node.args[0], node.children, strict=True)
        }
        policy = node.args[1]
        catchall = (
            None if node.args[2] is None else Schema[Any, Any](node.args[2])
        )
    properties: JSONSchema = {}
    required: list[str] = []
    for key, prop in shape.items():
        wire = _field_wire(prop._node)
        name = wire if io == "input" and wire is not None else key
        properties[name] = _export_schema(
            prop,
            (*path, "properties", name),
            target,
            io,
            unrepresentable,
            relax_strip,
        )
        if not _schema_accepts_missing(prop, io):
            required.append(name)
    result: JSONSchema = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    if catchall is not None:
        result["additionalProperties"] = _export_schema(
            catchall,
            (*path, "additionalProperties"),
            target,
            io,
            unrepresentable,
            relax_strip,
        )
    elif policy == "passthrough":
        result["additionalProperties"] = True
    elif policy == "strict" or (io == "output" and not relax_strip):
        result["additionalProperties"] = False
    return result


def _export_tuple(
    schema: Schema[Any, Any],
    node: NativeSchema,
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    if target == "openapi-3.0":
        return _unsupported(
            path,
            "tuples are not representable in OpenAPI 3.0",
            unrepresentable,
        )
    if isinstance(schema, TupleSchema):
        items = schema.items
        rest = schema._rest
    else:
        items = builtins.tuple(
            Schema[Any, Any](child) for child in node.children
        )
        rest_node = node.args[0]
        rest = None if rest_node is None else Schema[Any, Any](rest_node)
    prefix_items = [
        _export_schema(
            item,
            (*path, "prefixItems", index),
            target,
            io,
            unrepresentable,
            relax_strip,
        )
        for index, item in enumerate(items)
    ]
    if rest is not None:
        return {
            "type": "array",
            "prefixItems": prefix_items,
            "items": _export_schema(
                rest,
                (*path, "items"),
                target,
                io,
                unrepresentable,
                relax_strip,
            ),
            "minItems": len(items),
        }
    return {
        "type": "array",
        "prefixItems": prefix_items,
        "items": False,
        "minItems": len(items),
        "maxItems": len(items),
    }


def _export_union(
    schema: Schema[Any, Any],
    node: NativeSchema,
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    options = (
        schema.options
        if isinstance(schema, UnionSchema)
        else builtins.tuple(Schema[Any, Any](child) for child in node.children)
    )
    return {
        "anyOf": [
            _export_schema(
                option,
                (*path, "anyOf", index),
                target,
                io,
                unrepresentable,
                relax_strip,
            )
            for index, option in enumerate(options)
        ],
    }


def _export_discriminated_union(
    schema: Schema[Any, Any],
    node: NativeSchema,
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    if isinstance(schema, DiscriminatedUnionSchema):
        key = schema.key
        options = list(schema.options.items())
    else:
        key = node.args[0]
        options = [
            (label, Schema[Any, Any](child))
            for label, child in zip(node.args[1], node.children, strict=True)
        ]
    result: JSONSchema = {
        "oneOf": [
            _export_schema(
                option,
                (*path, "oneOf", label),
                target,
                io,
                unrepresentable,
                relax_strip,
            )
            for label, option in options
        ],
    }
    if target == "openapi-3.0":
        result["discriminator"] = {"propertyName": key}
    return result


def _apply_constraints(
    result: JSONSchema,
    constraints: list[NativeSchema],
    base_kind: str,
    target: JSONSchemaTarget,
    path: Path,
    unrepresentable: UnrepresentablePolicy,
) -> JSONSchema:
    lower: builtins.tuple[int | float, bool] | None = None
    upper: builtins.tuple[int | float, bool] | None = None
    multiple_values: list[int | float] = []
    min_length: int | None = None
    max_length: int | None = None
    min_items: int | None = None
    max_items: int | None = None
    integral = False
    regexes: list[NativeSchema] = []
    patterns: list[str] = []
    format_name: str | None = None
    extra_checks: list[JSONSchema] = []

    def stricter_lower(bound: int | float, inclusive: bool) -> None:
        nonlocal lower
        if (
            lower is None
            or bound > lower[0]
            or (bound == lower[0] and not inclusive)
        ):
            lower = (bound, inclusive)

    def stricter_upper(bound: int | float, inclusive: bool) -> None:
        nonlocal upper
        if (
            upper is None
            or bound < upper[0]
            or (bound == upper[0] and not inclusive)
        ):
            upper = (bound, inclusive)

    for node in constraints:
        kind = node.kind
        if kind in ("min", "max", "exclusive_min", "exclusive_max"):
            if base_kind not in ("number", "integer"):
                return _unsupported(
                    path, f"{kind} is not representable for {base_kind}",
                    unrepresentable,
                )
            bound = node.args[0]
            inclusive = kind in ("min", "max")
            if kind in ("min", "exclusive_min"):
                stricter_lower(bound, inclusive)
            else:
                stricter_upper(bound, inclusive)
        elif kind == "positive":
            if base_kind not in ("number", "integer"):
                return _unsupported(
                    path, f"{kind} is not representable for {base_kind}",
                    unrepresentable,
                )
            stricter_lower(0, False)
        elif kind == "multiple_of":
            if base_kind not in ("number", "integer"):
                return _unsupported(
                    path, f"{kind} is not representable for {base_kind}",
                    unrepresentable,
                )
            multiple_values.append(node.args[0])
        elif kind in ("length_min", "length_max"):
            if base_kind not in ("string", "array", "set"):
                return _unsupported(
                    path, f"{kind} is not representable for {base_kind}",
                    unrepresentable,
                )
            bound = node.args[0]
            if kind == "length_min":
                if base_kind == "string":
                    min_length = bound if min_length is None else max(min_length, bound)
                else:
                    min_items = bound if min_items is None else max(min_items, bound)
            elif base_kind == "string":
                max_length = bound if max_length is None else min(max_length, bound)
            else:
                max_items = bound if max_items is None else min(max_items, bound)
        elif kind == "integral":
            if base_kind not in ("number", "integer"):
                return _unsupported(
                    path, f"{kind} is not representable for {base_kind}",
                    unrepresentable,
                )
            integral = True
        elif kind == "regex":
            if base_kind != "string":
                return _unsupported(
                    path, "regex is only representable for strings",
                    unrepresentable,
                )
            regexes.append(node)
        elif kind in ("starts_with", "ends_with", "includes"):
            if base_kind != "string":
                return _unsupported(
                    path, f"{kind} is only representable for strings",
                    unrepresentable,
                )
            needle = node.args[0]
            if not isinstance(needle, str):
                return _unsupported(
                    path, f"{kind} is not representable", unrepresentable,
                )
            escaped = re.escape(needle)
            if kind == "starts_with":
                patterns.append("^" + escaped)
            elif kind == "ends_with":
                patterns.append(escaped + "$")
            else:
                patterns.append(escaped)
        elif kind == "ipv6":
            if base_kind != "string":
                return _unsupported(
                    path, "ipv6 is only representable for strings",
                    unrepresentable,
                )
            if format_name not in (None, "ipv6"):
                return _unsupported(
                    path, "multiple formats are not representable",
                    unrepresentable,
                )
            format_name = "ipv6"
        elif kind in ("cidrv4", "cidrv6"):
            if base_kind != "string":
                return _unsupported(
                    path, f"{kind} is only representable for strings",
                    unrepresentable,
                )
            if format_name not in (None, kind):
                return _unsupported(
                    path, "multiple formats are not representable",
                    unrepresentable,
                )
            format_name = kind
        else:
            return _unsupported(
                path, f"{kind} is not representable", unrepresentable,
            )

    if len(regexes) > 1:
        return _unsupported(
            path, "multiple regex constraints are not representable",
            unrepresentable,
        )
    if regexes:
        regex_node = regexes[0]
        compiled = regex_node.args[0]
        regex_format = regex_node.args[2]
        if not isinstance(compiled, re.Pattern):
            return _unsupported(
                path, "regex is not representable", unrepresentable,
            )
        if regex_format in _KNOWN_FORMATS:
            if format_name not in (None, regex_format):
                return _unsupported(
                    path, "multiple formats are not representable",
                    unrepresentable,
                )
            format_name = regex_format
        elif regex_format in _PATTERN_FORMATS:
            if not isinstance(compiled.pattern, str):
                return _unsupported(
                    path, "byte regexes are not representable",
                    unrepresentable,
                )
            if compiled.flags & ~re.UNICODE:
                return _unsupported(
                    path, "regex flags are not representable",
                    unrepresentable,
                )
            patterns.append(compiled.pattern)
        else:
            return _unsupported(
                path, "regex format is not representable", unrepresentable,
            )

    if base_kind in ("number", "integer"):
        if integral:
            result["type"] = "integer"
        if lower is not None:
            bound, inclusive = lower
            if target == "openapi-3.0":
                result["minimum"] = bound
                if not inclusive:
                    result["exclusiveMinimum"] = True
            elif inclusive:
                result["minimum"] = bound
            else:
                result["exclusiveMinimum"] = bound
        if upper is not None:
            bound, inclusive = upper
            if target == "openapi-3.0":
                result["maximum"] = bound
                if not inclusive:
                    result["exclusiveMaximum"] = True
            elif inclusive:
                result["maximum"] = bound
            else:
                result["exclusiveMaximum"] = bound
        if len(multiple_values) == 1:
            result["multipleOf"] = multiple_values[0]
        elif multiple_values:
            extra_checks.extend({"multipleOf": value} for value in multiple_values)
    elif base_kind == "string":
        if min_length is not None:
            result["minLength"] = min_length
        if max_length is not None:
            result["maxLength"] = max_length
        if format_name is not None:
            result["format"] = format_name
        if len(patterns) == 1:
            result["pattern"] = patterns[0]
        elif patterns:
            extra_checks.extend({"pattern": pattern} for pattern in patterns)
    elif base_kind in ("array", "set"):
        if min_items is not None:
            result["minItems"] = min_items
        if max_items is not None:
            result["maxItems"] = max_items
    elif constraints:
        return _unsupported(
            path, f"constraints on {base_kind} are not representable",
            unrepresentable,
        )

    if extra_checks:
        existing = result.get("allOf")
        if isinstance(existing, list):
            result["allOf"] = [*existing, *extra_checks]
        else:
            result["allOf"] = extra_checks
    return result
