"""Validated dataclass decorator and constructor assembly.

The decorator compiles field annotations into one native object schema, then
installs an initializer that validates decoded field values in a single native
evaluation. Codec decoding for decorated classes reuses the same schemas and
assembler without calling the validating initializer.
"""

from __future__ import annotations

import builtins
import dataclasses
import functools
import inspect
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass as _dataclass
from typing import Any

from . import _annotations
from . import schemas as _schemas
from ._codecs import CodecSchema
from ._core import _UNSET, MISSING, Schema
from ._native import NativeSchema


_MARKER_ATTR = "__zodish_validated_dataclass__"
_UNSUPPORTED_CONSTRUCTION_KINDS = frozenset({
    "preprocess", "transform", "coerce", "string_bool", "pipe",
})
_SCALAR_KINDS = frozenset({"string", "integer", "number", "boolean", "none"})
_INIT_PARAMETER_KINDS = (
    inspect.Parameter.POSITIONAL_ONLY,
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.KEYWORD_ONLY,
)


_INITVAR_FIELD = getattr(dataclasses, "_FIELD_INITVAR", None)


def _is_initvar_field(field: dataclasses.Field[Any]) -> bool:
    return getattr(field, "_field_type", None) is _INITVAR_FIELD


def _initvar_annotation(annotation: Any, name: str) -> Any:
    if isinstance(annotation, dataclasses.InitVar):
        return annotation.type
    raise TypeError(f"Field {name!r} has an invalid InitVar annotation")


@_dataclass(frozen=True)
class _ValidatedDataclass:
    """Private metadata attached to the exact decorated class."""

    cls: type[Any]
    stored_names: builtins.tuple[str, ...]
    initvar_names: builtins.tuple[str, ...]
    non_init_names: builtins.tuple[str, ...]
    constructor_names: builtins.tuple[str, ...]
    construction_schemas: Mapping[str, Schema[Any, Any]]
    guards: Mapping[str, Schema[Any, Any] | None]
    annotation_guards: Mapping[str, Schema[Any, Any] | None]
    fields: Mapping[str, dataclasses.Field[Any]]
    has_default: Mapping[str, bool]


def marker_for(cls: type[Any]) -> _ValidatedDataclass | None:
    """Return validation metadata for the exact class, never an inherited one."""
    marker = cls.__dict__.get(_MARKER_ATTR)
    return marker if isinstance(marker, _ValidatedDataclass) else None


def codec_for(cls: type[Any]) -> CodecSchema[Any, Any]:
    """Return the cached dataclass codec for a validated class."""
    if marker_for(cls) is None:
        raise TypeError("ValidatedModel methods require @z.dataclass")
    cached = cls.__dict__.get("__zodish_codec_cache__")
    if isinstance(cached, CodecSchema):
        return cached
    codec = _schemas.codecs.dataclass(cls)
    type.__setattr__(cls, "__zodish_codec_cache__", codec)
    return codec


def assemble_validated(
    cls: type[Any],
    values: Mapping[str, Any],
    initvar_values: Sequence[Any] = (),
) -> Any:
    """Allocate and populate an instance without calling its initializer."""
    instance = object.__new__(cls)
    _assign(instance, values, initvar_values)
    return instance


def default_value(field: dataclasses.Field[Any]) -> Any:
    """Resolve one omitted dataclass field; factories run exactly once."""
    if field.default is not dataclasses.MISSING:
        if field.default is MISSING:
            raise TypeError(
                f"Field {field.name!r} cannot use MISSING as a default",
            )
        return field.default
    if field.default_factory is not dataclasses.MISSING:
        value = field.default_factory()
        if value is MISSING:
            raise TypeError(
                f"Field {field.name!r} factory produced MISSING",
            )
        return value
    raise TypeError(f"Field {field.name!r} has no default value")


def _assign(
    instance: Any,
    values: Mapping[str, Any],
    initvar_values: Sequence[Any] = (),
) -> None:
    for name, value in values.items():
        object.__setattr__(instance, name, value)
    post_init = getattr(instance, "__post_init__", None)
    if post_init is not None:
        post_init(*initvar_values)


def _contains_kind(node: NativeSchema, kinds: frozenset[str]) -> bool:
    if node.kind in kinds:
        return True
    if builtins.any(_contains_kind(child, kinds) for child in node.children):
        return True
    for arg in node.args:
        if isinstance(arg, NativeSchema):
            if _contains_kind(arg, kinds):
                return True
        elif isinstance(arg, (tuple, list)):
            if builtins.any(
                isinstance(item, NativeSchema)
                and _contains_kind(item, kinds)
                for item in arg
            ):
                return True
        elif isinstance(arg, Mapping) and builtins.any(
            isinstance(item, NativeSchema)
            and _contains_kind(item, kinds)
            for item in arg.values()
        ):
            return True
    return False


def _annotation_text(annotation: Any) -> str:
    if annotation is Any:
        return "Any"
    if annotation is None or annotation is type(None):
        return "None"
    if isinstance(annotation, type):
        return annotation.__name__
    return repr(annotation)


def _check_static_output(
    annotation: Any,
    base: Schema[Any, Any],
    name: str,
    guard: Schema[Any, Any],
) -> None:
    """Reject output kinds that are clearly incompatible before construction."""
    guard_kind = guard._node.kind
    schema_kind = base._node.kind
    if guard_kind in _SCALAR_KINDS and schema_kind in _SCALAR_KINDS:
        if guard_kind == schema_kind:
            return
        if guard_kind == "number" and schema_kind == "integer":
            return
    elif (
        (guard_kind == "exact_instance" and schema_kind in _SCALAR_KINDS)
        or (schema_kind == "exact_instance" and guard_kind in _SCALAR_KINDS)
    ):
        pass
    else:
        return
    raise TypeError(
        f"Field {name!r} schema produces {schema_kind}, incompatible with "
        f"annotation {_annotation_text(annotation)}",
    )


def _codec_output_schema(schema: Schema[Any, Any]) -> Schema[Any, Any] | None:
    """Return a codec's decoded schema, looking through a wire wrapper."""
    current = schema
    while current._node.kind == "wire" and current._inner is not None:
        current = current._inner
    if isinstance(current, CodecSchema):
        return current.output_schema
    return None


def _guarded_schema(
    base: Schema[Any, Any],
    guard: Schema[Any, Any],
    name: str,
    expected: str,
) -> Schema[Any, Any]:
    return Schema[Any, Any](
        NativeSchema("output_guard", (base._node, guard._node), (name, expected)),
        _inner=base,
        _meta=base._meta,
    )


def _guard_only_schema(
    guard: Schema[Any, Any],
    name: str,
    expected: str,
) -> Schema[Any, Any]:
    return Schema[Any, Any](
        NativeSchema(
            "output_guard",
            (_schemas.unknown()._node, guard._node),
            (name, expected),
        ),
        _inner=guard,
    )


def _reject_unsupported_inheritance(cls: type[Any]) -> None:
    direct_bases = [
        base
        for base in cls.__bases__
        if dataclasses.is_dataclass(base) and dataclasses.fields(base)
    ]
    if len(direct_bases) > 1:
        raise TypeError(
            "Validated dataclasses support at most one dataclass base",
        )
    for base in cls.__mro__[1:]:
        if base is object or not dataclasses.is_dataclass(base):
            continue
        base_params = getattr(base, "__dataclass_params__", None)
        if base_params is not None and not base_params.init:
            raise TypeError(
                "Validated dataclasses do not support a dataclass base "
                "with init=False",
            )
        if marker_for(base) is not None:
            continue
        init = base.__dict__.get("__init__")
        if init is not None and not _is_generated_init(init):
            raise TypeError(
                "Validated dataclasses do not support a dataclass base "
                "with a custom __init__",
            )
    if getattr(cls, "__parameters__", ()):
        raise TypeError(
            "Validated dataclasses do not support generic classes",
        )
    for base in getattr(cls, "__orig_bases__", ()):
        origin = typing.get_origin(base)
        if origin is not None and dataclasses.is_dataclass(origin):
            raise TypeError(
                "Validated dataclasses do not support parameterized "
                "dataclass bases",
            )


def _reject_unsupported(cls: type[Any]) -> None:
    params = getattr(cls, "__dataclass_params__", None)
    if params is not None and not params.init:
        raise TypeError("Validated dataclasses require init=True")
    _reject_unsupported_inheritance(cls)
    initvar_fields = [
        field
        for field in getattr(cls, "__dataclass_fields__", {}).values()
        if _is_initvar_field(field)
    ]
    post_init = getattr(cls, "__post_init__", None)
    if post_init is not None:
        parameters = list(inspect.signature(post_init).parameters.values())
        if parameters and parameters[0].name == "self":
            parameters = parameters[1:]
        if (
            len(parameters) != len(initvar_fields)
            or builtins.any(
                parameter.kind not in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
                for parameter in parameters
            )
        ):
            raise TypeError(
                "Validated dataclasses require __post_init__ to accept "
                "exactly the InitVar fields",
            )


def _checked_value(name: str, value: Any) -> Any:
    if value is MISSING:
        raise TypeError(
            f"MISSING is not a valid value for field {name!r}",
        )
    return value


def _resolved_values(
    bound: inspect.BoundArguments,
    constructor_fields: Mapping[str, dataclasses.Field[Any]],
    non_init_fields: Mapping[str, dataclasses.Field[Any]],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, field in constructor_fields.items():
        if name in bound.arguments:
            values[name] = _checked_value(name, bound.arguments[name])
        elif field.default is not dataclasses.MISSING:
            values[name] = _checked_value(name, field.default)
        elif field.default_factory is not dataclasses.MISSING:
            values[name] = _checked_value(name, field.default_factory())
        else:
            raise TypeError(f"Missing required argument: {name!r}")
    for name, field in non_init_fields.items():
        if field.default is not dataclasses.MISSING:
            values[name] = _checked_value(name, field.default)
        elif field.default_factory is not dataclasses.MISSING:
            values[name] = _checked_value(name, field.default_factory())
    return values


def _is_generated_init(init: Any) -> bool:
    code = getattr(init, "__code__", None)
    return code is not None and code.co_filename == "<string>"


def _reject_allocation_hooks(cls: type[Any]) -> None:
    if getattr(cls, "__new__", object.__new__) is not object.__new__:
        raise TypeError(
            "Validated dataclasses do not support a custom __new__",
        )
    dataclass_params = getattr(cls, "__dataclass_params__", None)
    frozen = bool(dataclass_params is not None and dataclass_params.frozen)
    if (
        not frozen
        and getattr(cls, "__setattr__", object.__setattr__)
        is not object.__setattr__
    ):
        raise TypeError(
            "Validated dataclasses do not support a custom __setattr__",
        )


_CLASSMETHOD_NAMES = (
    "parse",
    "safe_parse",
    "validate",
    "parse_json",
    "safe_parse_json",
    "validate_json",
    "parse_async",
    "safe_parse_async",
    "validate_async",
    "parse_json_async",
    "safe_parse_json_async",
    "encode",
    "safe_encode",
    "encode_async",
    "safe_encode_async",
    "to_json_schema",
)


def _install_classmethods(
    cls: type[Any],
    field_names: builtins.set[str],
) -> None:
    for name in _CLASSMETHOD_NAMES:
        if name in field_names or name in cls.__dict__:
            continue
        if builtins.any(name in base.__dict__ for base in cls.__mro__[1:]):
            continue
        descriptor = inspect.getattr_static(_schemas.ValidatedModel, name)
        if isinstance(descriptor, classmethod):
            setattr(cls, name, classmethod(descriptor.__func__))


def _decorate(cls: type[Any], options: Mapping[str, Any]) -> type[Any]:
    if marker_for(cls) is not None:
        if options:
            raise TypeError(
                "Applied options to an already validated dataclass",
            )
        return cls

    own_dataclass = "__dataclass_fields__" in cls.__dict__
    if own_dataclass:
        if options:
            raise TypeError(
                "Applied options to an existing dataclass; use a bare "
                "@z.dataclass wrapper for an existing dataclass",
            )
        init = cls.__dict__.get("__init__")
        if init is not None and not _is_generated_init(init):
            raise TypeError(
                "Validated dataclasses do not support a custom __init__",
            )
    else:
        if options.get("init", True) is False:
            raise TypeError("Validated dataclasses require init=True")
        if options.get("weakref_slot", False) and not options.get("slots", False):
            raise TypeError("weakref_slot=True requires slots=True")
        if "__init__" in cls.__dict__:
            raise TypeError(
                "Validated dataclasses do not support a custom __init__",
            )
        cls = dataclasses.dataclass(cls, **options)

    _reject_unsupported(cls)
    _reject_allocation_hooks(cls)

    init_signature = inspect.signature(cls.__init__)
    init_parameters = list(init_signature.parameters.values())
    if init_parameters and init_parameters[0].name == "self":
        init_parameters = init_parameters[1:]
    signature = init_signature.replace(parameters=init_parameters)
    all_fields = cls.__dataclass_fields__
    regular_init_fields = [
        field for field in dataclasses.fields(cls) if field.init
    ]
    initvar_fields = [
        field for field in all_fields.values() if _is_initvar_field(field)
    ]
    constructor_fields = {
        name: field
        for name, field in all_fields.items()
        if _is_initvar_field(field) or field.init
    }
    non_init_default_fields = {
        field.name: field
        for field in dataclasses.fields(cls)
        if not field.init
        and (
            field.default is not dataclasses.MISSING
            or field.default_factory is not dataclasses.MISSING
        )
    }
    stored_names = builtins.tuple(
        field.name for field in regular_init_fields
    )
    initvar_names = builtins.tuple(
        field.name for field in initvar_fields
    )
    non_init_names = builtins.tuple(non_init_default_fields)
    constructor_names = builtins.tuple(constructor_fields)
    expected = builtins.set(constructor_names)
    parameters = {
        name
        for name, parameter in signature.parameters.items()
        if parameter.kind in _INIT_PARAMETER_KINDS
    }
    if parameters - expected:
        raise TypeError(
            "Unsupported dataclass init parameters: "
            + ", ".join(sorted(parameters - expected)),
        )
    if expected - parameters:
        raise TypeError(
            "Unsupported dataclass init fields: "
            + ", ".join(sorted(expected - parameters)),
        )

    hints = _annotations.dataclass_type_hints(cls)
    construction_schemas: dict[str, Schema[Any, Any]] = {}
    guards: dict[str, Schema[Any, Any] | None] = {}
    annotation_guards: dict[str, Schema[Any, Any] | None] = {}
    has_default: dict[str, bool] = {}
    schema_fields = builtins.tuple(constructor_fields.items()) + builtins.tuple(
        non_init_default_fields.items()
    )
    for name, field in schema_fields:
        annotation = hints.get(name, _UNSET)
        if annotation is _UNSET:
            raise TypeError(f"Cannot infer a schema for field {name!r}")
        if _is_initvar_field(field):
            annotation = _initvar_annotation(annotation, name)
        schema, resolved, explicit = _annotations.dataclass_field_schema(
            annotation, name, stack=(cls,),
        )
        if not isinstance(schema, Schema):
            raise TypeError(f"Field {name!r} must be a schema")
        base = _codec_output_schema(schema) or schema
        if base._node.has_async:
            raise TypeError(
                f"Field {name!r} uses asynchronous validation; "
                "construction is synchronous",
            )
        if _contains_kind(base._node, _UNSUPPORTED_CONSTRUCTION_KINDS):
            raise TypeError(
                f"Field {name!r} uses a type-changing conversion; "
                "constructor values are decoded values. Use a codec or the "
                "codec boundary instead",
            )
        field_has_default = (
            field.default is not dataclasses.MISSING
            or field.default_factory is not dataclasses.MISSING
        )
        if field.default is MISSING:
            raise TypeError(
                f"Field {name!r} cannot use MISSING as a default",
            )
        if not field_has_default and base._node.kind == "optional":
            raise ValueError(
                f"Field {name!r} accepts missing input but the dataclass "
                "field has no default",
            )
        candidate = _annotations.infer_dataclass_field(
            annotation, name, stack=(cls,), type_only=True,
        )
        expected_text = _annotation_text(resolved)
        annotation_guard = (
            None
            if candidate._node.kind == "unknown"
            else _guard_only_schema(candidate, name, expected_text)
        )
        guard: Schema[Any, Any] | None = None
        construction = base
        if explicit:
            _check_static_output(annotation, base, name, candidate)
            if annotation_guard is not None:
                construction = _guarded_schema(
                    base, candidate, name, expected_text,
                )
                guard = annotation_guard
        construction_schemas[name] = construction
        guards[name] = guard
        annotation_guards[name] = annotation_guard
        has_default[name] = field_has_default

    constructor_schema = _schemas.object(construction_schemas)
    marker = _ValidatedDataclass(
        cls=cls,
        stored_names=stored_names,
        initvar_names=initvar_names,
        non_init_names=non_init_names,
        constructor_names=constructor_names,
        construction_schemas=construction_schemas,
        guards=guards,
        annotation_guards=annotation_guards,
        fields={name: field for name, field in all_fields.items()},
        has_default=has_default,
    )
    original_init = cls.__init__

    @functools.wraps(original_init)
    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
        bound = signature.bind(*args, **kwargs)
        values = _resolved_values(
            bound, constructor_fields, non_init_default_fields,
        )
        parsed = constructor_schema.parse(values)
        missing_outputs = [
            name
            for name in construction_schemas
            if name not in parsed or parsed[name] is MISSING
        ]
        if missing_outputs:
            raise TypeError(
                "Fields produced MISSING: "
                + ", ".join(builtins.map(repr, missing_outputs)),
            )
        initvar_values = [parsed[name] for name in initvar_names]
        stored_values = {
            name: parsed[name]
            for name in (*stored_names, *non_init_names)
        }
        _assign(self, stored_values, initvar_values)

    setattr(cls, _MARKER_ATTR, marker)
    cls.__init__ = __init__
    cls.__signature__ = signature
    _install_classmethods(cls, builtins.set(all_fields))
    return cls


def dataclass(
    cls: type[Any] | None = None,
    /,
    **options: Any,
) -> Any:
    """Implement the public ``z.dataclass`` decorator."""
    def decorate(target: type[Any]) -> type[Any]:
        return _decorate(target, options)

    return decorate if cls is None else decorate(cls)
