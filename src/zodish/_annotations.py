"""Private dataclass annotation resolution and schema inference.

Only schema construction happens here; validation uses the native schema nodes.
The dataclass codec imports this module on demand after schema builders exist.
"""

from __future__ import annotations

import annotationlib
import builtins
import dataclasses
import datetime as _datetime
import decimal as _decimal
import pathlib as _pathlib
import types
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, cast

from . import schemas as _schemas


@dataclass(frozen=True)
class _DataclassAnnotation:
    annotation: Any
    aliases: builtins.tuple[typing.TypeAliasType, ...] = ()
    bindings: Mapping[typing.TypeVar, _DataclassAnnotation] = field(default_factory=dict)


def dataclass_type_hints(cls: type[Any]) -> dict[str, Any]:
    """Resolve annotations without discarding schema metadata."""
    try:
        return typing.get_type_hints(cls, include_extras=True)
    except (NameError, TypeError) as error:
        raise TypeError("Cannot resolve dataclass annotations") from error


def typed_dict_keys(typ: type[Any]) -> builtins.tuple[frozenset[str], frozenset[str]]:
    """Correct key qualifiers that TypedDict metadata can miss when postponed."""
    required = builtins.set(getattr(typ, "__required_keys__", ()))
    optional = builtins.set(getattr(typ, "__optional_keys__", ()))
    annotations = annotationlib.get_annotations(typ, format=annotationlib.Format.FORWARDREF)
    for name, raw_annotation in annotations.items():
        annotation: Any = raw_annotation
        if isinstance(annotation, str):
            annotation = typing.ForwardRef(annotation)
        if isinstance(annotation, typing.ForwardRef):
            annotation = typing.evaluate_forward_ref(
                annotation, owner=typ, format=annotationlib.Format.FORWARDREF,
            )
        while typing.get_origin(annotation) in (typing.Annotated, typing.ReadOnly):
            annotation = typing.get_args(annotation)[0]
        origin = typing.get_origin(annotation)
        if origin is typing.Required:
            required.add(name)
            optional.discard(name)
        elif origin is typing.NotRequired:
            optional.add(name)
            required.discard(name)
    return frozenset(required), frozenset(optional)


def _resolve_annotation(
    context: _DataclassAnnotation,
    name: str,
) -> builtins.tuple[
    _DataclassAnnotation, list[_schemas.Schema[Any, Any]],
]:
    annotation = context.annotation
    aliases = context.aliases
    bindings = context.bindings
    metadata: list[_schemas.Schema[Any, Any]] = []
    while True:
        if isinstance(annotation, typing.TypeVar):
            bound = bindings.get(annotation)
            if bound is None:
                raise TypeError(
                    f"Unbound type parameter {annotation!r} "
                    f"for field {name!r}",
                )
            # Arguments retain their caller's scope. In particular,
            # Items[Items[int]] is finite, whereas Items[T] appearing
            # in Items's own definition is recursive.
            annotation = bound.annotation
            aliases = bound.aliases
            bindings = bound.bindings
            continue
        origin = typing.get_origin(annotation)
        args = typing.get_args(annotation)
        if origin is typing.Annotated:
            metadata.extend(
                item for item in args[1:] if isinstance(item, _schemas.Schema)
            )
            annotation = args[0]
            continue
        alias = (
            annotation if isinstance(annotation, typing.TypeAliasType)
            else origin if isinstance(origin, typing.TypeAliasType)
            else None
        )
        if alias is None:
            break
        if alias in aliases:
            raise TypeError(
                f"Recursive type aliases are not supported "
                f"for field {name!r}",
            )
        parameters = alias.__type_params__
        if any(not isinstance(item, typing.TypeVar) for item in parameters):
            raise TypeError(
                f"Variadic type alias parameters are not supported "
                f"for field {name!r}; use fields= to supply a schema",
            )
        if len(args) > len(parameters):
            raise TypeError(
                f"Too many type arguments for alias {alias.__name__!r} "
                f"in field {name!r}",
            )
        alias_bindings = dict(bindings)
        try:
            for index, parameter in enumerate(parameters):
                parameter = cast(typing.TypeVar, parameter)
                if index < len(args):
                    bound = _DataclassAnnotation(args[index], aliases, bindings)
                elif parameter.has_default():
                    # Defaults may refer to earlier parameters of this
                    # alias, so capture those bindings as well.
                    bound = _DataclassAnnotation(
                        parameter.__default__, (*aliases, alias),
                        dict(alias_bindings),
                    )
                else:
                    raise TypeError(
                        f"Missing type argument {parameter.__name__!r} "
                        f"for alias {alias.__name__!r} in field {name!r}",
                    )
                alias_bindings[parameter] = bound
            annotation = alias.__value__
        except NameError as error:
            raise TypeError(
                f"Cannot resolve type alias for field {name!r}",
            ) from error
        aliases = (*aliases, alias)
        bindings = alias_bindings
    if len(metadata) > 1:
        raise TypeError(
            f"Field {name!r} has multiple Annotated schemas; "
            "combine them into one schema",
        )
    return _DataclassAnnotation(annotation, aliases, bindings), metadata


def infer_dataclass_field(
    annotation: Any,
    name: str,
    *,
    stack: builtins.tuple[type[Any], ...],
    context: _DataclassAnnotation | None = None,
) -> _schemas.Schema[Any, Any]:
    """Build a field schema while retaining alias bindings and dataclass ancestry."""
    context, metadata = _resolve_annotation(
        _DataclassAnnotation(annotation) if context is None
        else replace(context, annotation=annotation), name,
    )
    if metadata:
        return metadata[0]
    annotation = context.annotation

    def child(item: Any) -> _schemas.Schema[Any, Any]:
        return infer_dataclass_field(item, name, stack=stack, context=context)

    if annotation is Any or annotation is builtins.object:
        return _schemas.unknown()
    if annotation is None or annotation is type(None):
        return _schemas.none()
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Literal:
        choices = builtins.tuple(args)
        return (
            _schemas.literal(choices[0]) if len(choices) == 1 else _schemas.enum(choices)
        )
    if origin in (types.UnionType, typing.Union):
        non_none = [
            item for item in args if item is not type(None)
        ]
        options = [child(item) for item in non_none]
        if not options:
            return _schemas.none()
        if len(options) == 1:
            combined: _schemas.Schema[Any, Any] = options[0]
        elif any(isinstance(item, _schemas.CodecSchema) for item in options):
            combined = _schemas._union_codec(options)
        else:
            combined = _schemas.union(*options)
        if len(non_none) != len(args):
            combined = (
                _schemas._nullable_codec(combined)
                if isinstance(combined, _schemas.CodecSchema)
                else combined.nullable()
            )
        return combined
    if origin is list or origin is Sequence:
        item = child(args[0]) if args else _schemas.unknown()
        return (
            _schemas._array_codec(item)
            if isinstance(item, _schemas.CodecSchema)
            else _schemas.array(item)
        )
    if origin is dict or origin is Mapping:
        if not args:
            return _schemas.record(_schemas.unknown())
        key_type, value_type = args[0], args[1]
        key_context, key_metadata = _resolve_annotation(
            replace(context, annotation=key_type), name,
        )
        if key_context.annotation is not str or key_metadata:
            raise TypeError(
                f"Field {name!r} must use string dictionary keys",
            )
        value = child(value_type)
        return (
            _schemas._record_codec(value)
            if isinstance(value, _schemas.CodecSchema)
            else _schemas.record(value)
        )
    if origin is builtins.tuple:
        if annotation is builtins.tuple:
            raise TypeError(
                f"Field {name!r} uses a bare tuple annotation; "
                "use fixed-length tuple[...] syntax",
            )
        if args and args[-1] is Ellipsis:
            raise TypeError(
                f"Field {name!r} uses a variable-length tuple; "
                "use a fixed-length tuple[...] annotation",
            )
        if not args:
            return _schemas.tuple()
        items = [child(item) for item in args]
        return (
            _schemas._tuple_codec(items)
            if any(isinstance(item, _schemas.CodecSchema) for item in items)
            else _schemas.tuple(*items)
        )
    if annotation is list or annotation is Sequence:
        return _schemas.array(_schemas.unknown())
    if annotation is dict or annotation is Mapping:
        return _schemas.record(_schemas.unknown())
    if annotation is builtins.tuple:
        raise TypeError(
            f"Field {name!r} uses a bare tuple annotation; "
            "use fixed-length tuple[...] syntax",
        )
    if not isinstance(annotation, type):
        supertype = getattr(annotation, "__supertype__", None)
        if supertype is not None:
            return child(supertype)
        raise TypeError(
            f"Cannot infer a schema for field {name!r}: "
            f"{annotation!r}",
        )
    if dataclasses.is_dataclass(annotation):
        return _schemas._Codecs.dataclass(annotation, _stack=stack)
    if issubclass(annotation, Enum):
        return _schemas.native_enum(annotation)
    if issubclass(annotation, bool):
        return _schemas.boolean()
    if issubclass(annotation, int):
        return _schemas.integer()
    if issubclass(annotation, float):
        return _schemas.number()
    if issubclass(annotation, str):
        return _schemas.string()
    if issubclass(annotation, builtins.bytes):
        return _schemas.bytes()
    if issubclass(annotation, _datetime.datetime):
        return _schemas.datetime()
    if issubclass(annotation, _datetime.date):
        return _schemas.date()
    if issubclass(annotation, _datetime.time):
        return _schemas.time()
    if issubclass(annotation, _datetime.timedelta):
        return _schemas.timedelta()
    if issubclass(annotation, _decimal.Decimal):
        return _schemas.decimal()
    if issubclass(annotation, _pathlib.PurePath):
        return _schemas.path()
    raise TypeError(
        f"Cannot infer a schema for field {name!r}: {annotation!r}",
    )
