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
import sys
import types
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, cast

from . import _codecs as _codecs
from . import schemas as _schemas


@dataclass(frozen=True)
class _DataclassAnnotation:
    annotation: Any
    aliases: builtins.tuple[typing.TypeAliasType, ...] = ()
    bindings: Mapping[typing.TypeVar, _DataclassAnnotation] = field(default_factory=dict)


def _fallback_type_hints(cls: type[Any]) -> dict[str, Any]:
    """Resolve deferred self-references that ``get_type_hints`` cannot.

    PEP 649 evaluates annotations from the class's ``__annotate__`` function.
    Before a decorator assigns the class to its module name, that function can
    still fail on a self-reference. Resolve each MRO class with its class and
    base names in the local namespace, preserving inherited annotations.
    """
    localns = {base.__name__: base for base in cls.__mro__}
    resolved: dict[str, Any] = {}
    for base in reversed(cls.__mro__):
        if base is object:
            continue
        raw = annotationlib.get_annotations(
            base, format=annotationlib.Format.FORWARDREF,
        )
        module = sys.modules.get(base.__module__)
        globalns = vars(module) if module is not None else {}
        base_localns = dict(localns)
        base_localns.update(vars(base))
        for name, annotation in raw.items():
            if isinstance(annotation, str):
                annotation = typing.ForwardRef(annotation)
            resolved[name] = typing._eval_type(  # type: ignore[attr-defined]
                annotation,
                globalns,
                base_localns,
                type_params=getattr(base, "__type_params__", ()),
                owner=base,
            )
    return resolved


def dataclass_type_hints(cls: type[Any]) -> dict[str, Any]:
    """Resolve annotations without discarding schema metadata."""
    try:
        return typing.get_type_hints(
            cls,
            localns={cls.__name__: cls},
            include_extras=True,
        )
    except (NameError, TypeError):
        try:
            return _fallback_type_hints(cls)
        except (NameError, TypeError) as fallback_error:
            raise TypeError(
                "Cannot resolve dataclass annotations",
            ) from fallback_error


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
    seen_bindings: set[tuple[typing.TypeVar, int]] = set()
    while True:
        if isinstance(annotation, typing.TypeVar):
            key = (annotation, id(bindings))
            if key in seen_bindings:
                raise TypeError(f"Cyclic type parameter binding for field {name!r}")
            seen_bindings.add(key)
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
    type_only: bool = False,
    decoded_dataclasses: bool = False,
    self_codec: _codecs.CodecSchema[Any, Any] | None = None,
    self_type: type[Any] | None = None,
) -> _schemas.Schema[Any, Any]:
    """Build a field schema while retaining alias bindings and dataclass ancestry.

    ``type_only`` ignores ``Annotated`` schemas and compiles a type-shape guard
    for already-decoded values instead of a conversion codec.
    """
    context, metadata = _resolve_annotation(
        _DataclassAnnotation(annotation) if context is None
        else replace(context, annotation=annotation), name,
    )
    if metadata and not type_only:
        return metadata[0]
    annotation = context.annotation

    def child(item: Any) -> _schemas.Schema[Any, Any]:
        return infer_dataclass_field(
            item,
            name,
            stack=stack,
            context=context,
            type_only=type_only,
            decoded_dataclasses=decoded_dataclasses,
            self_codec=self_codec,
            self_type=self_type,
        )

    if self_codec is not None and self_type is not None:
        origin = typing.get_origin(annotation)
        if annotation is self_type or origin is self_type:
            return self_codec
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
        elif type_only or not any(
            isinstance(item, _codecs.CodecSchema) for item in options
        ):
            combined = _schemas.union(*options)
        else:
            combined = _codecs._union_codec(options)
        if len(non_none) != len(args):
            combined = (
                _codecs._nullable_codec(combined)
                if isinstance(combined, _codecs.CodecSchema)
                else combined.nullable()
            )
        return combined
    if origin is list or origin is Sequence:
        item = child(args[0]) if args else _schemas.unknown()
        if type_only or not isinstance(item, _codecs.CodecSchema):
            return _schemas.array(item)
        return _codecs._array_codec(item)
    if origin is dict or origin is Mapping:
        if not args:
            return _schemas.record(_schemas.unknown())
        key_type, value_type = args[0], args[1]
        if type_only:
            return _schemas.record(child(key_type), child(value_type))
        key_context, key_metadata = _resolve_annotation(
            replace(context, annotation=key_type), name,
        )
        if key_context.annotation is not str or key_metadata:
            raise TypeError(
                f"Field {name!r} must use string dictionary keys",
            )
        value = child(value_type)
        return (
            _codecs._record_codec(value)
            if isinstance(value, _codecs.CodecSchema)
            else _schemas.record(value)
        )
    if type_only and origin is builtins.set:
        return _schemas.set(child(args[0]) if args else _schemas.unknown())
    if type_only and origin is builtins.frozenset:
        return _schemas.instance_of(builtins.frozenset)
    if origin is builtins.tuple:
        if annotation is builtins.tuple:
            if type_only:
                return _schemas.instance_of(builtins.tuple)
            raise TypeError(
                f"Field {name!r} uses a bare tuple annotation; "
                "use fixed-length tuple[...] syntax",
            )
        if args and args[-1] is Ellipsis:
            if type_only:
                item = child(args[0]) if len(args) > 1 else _schemas.unknown()
                return _schemas.instance_of(builtins.tuple).pipe(
                    _schemas.tuple().rest(item),
                )
            raise TypeError(
                f"Field {name!r} uses a variable-length tuple; "
                "use a fixed-length tuple[...] annotation",
            )
        items_schema: _schemas.Schema[Any, Any]
        if not args:
            items_schema = _schemas.tuple()
        else:
            items = [child(item) for item in args]
            items_schema = (
                _schemas.tuple(*items)
                if type_only or not any(
                    isinstance(item, _codecs.CodecSchema) for item in items
                )
                else _codecs._tuple_codec(items)
            )
        if type_only:
            return _schemas.instance_of(builtins.tuple).pipe(items_schema)
        return items_schema
    if annotation is list or annotation is Sequence:
        return _schemas.array(_schemas.unknown())
    if annotation is dict or annotation is Mapping:
        return _schemas.record(_schemas.unknown())
    if type_only and annotation is builtins.set:
        return _schemas.set(_schemas.unknown())
    if type_only and annotation is builtins.frozenset:
        return _schemas.instance_of(builtins.frozenset)
    if annotation is builtins.tuple:
        if type_only:
            return _schemas.instance_of(builtins.tuple)
        raise TypeError(
            f"Field {name!r} uses a bare tuple annotation; "
            "use fixed-length tuple[...] syntax",
        )
    if (
        (type_only or decoded_dataclasses)
        and origin is not None
        and dataclasses.is_dataclass(origin)
    ):
        return _schemas.instance_of(cast(type[Any], origin))
    if not isinstance(annotation, type):
        supertype = getattr(annotation, "__supertype__", None)
        if supertype is not None:
            return child(supertype)
        raise TypeError(
            f"Cannot infer a schema for field {name!r}: "
            f"{annotation!r}",
        )
    if type_only and typing.is_typeddict(annotation):
        return _schemas.record(_schemas.unknown())
    if dataclasses.is_dataclass(annotation):
        if type_only or decoded_dataclasses:
            return _schemas.instance_of(annotation)
        return _codecs._Codecs.dataclass(annotation, _stack=stack)
    if issubclass(annotation, Enum):
        return _schemas.native_enum(annotation)
    if annotation is bool:
        return _schemas.boolean()
    if annotation is int:
        return _schemas.integer()
    if annotation is float:
        return _schemas.number()
    if annotation is str:
        return _schemas.string()
    if annotation is builtins.bytes:
        return _schemas.bytes()
    if annotation is _datetime.datetime:
        return _schemas.datetime()
    if annotation is _datetime.date:
        return _schemas.date()
    if annotation is _datetime.time:
        return _schemas.time()
    if annotation is _datetime.timedelta:
        return _schemas.timedelta()
    if annotation is _decimal.Decimal:
        return _schemas.decimal()
    if annotation is _pathlib.PurePath:
        return _schemas.path()
    if type_only:
        return _schemas.instance_of(annotation)
    if issubclass(annotation, bool):
        return _schemas.boolean()
    if issubclass(annotation, int):
        return _schemas.instance_of(annotation)
    if issubclass(annotation, float):
        return _schemas.instance_of(annotation)
    if issubclass(annotation, str):
        return _schemas.instance_of(annotation)
    if issubclass(annotation, builtins.bytes):
        return _schemas.instance_of(annotation)
    if issubclass(annotation, _datetime.datetime):
        return _schemas.instance_of(annotation)
    if issubclass(annotation, _datetime.date):
        return _schemas.instance_of(annotation)
    if issubclass(annotation, _datetime.time):
        return _schemas.instance_of(annotation)
    if issubclass(annotation, _datetime.timedelta):
        return _schemas.instance_of(annotation)
    if issubclass(annotation, _decimal.Decimal):
        return _schemas.instance_of(annotation)
    if issubclass(annotation, _pathlib.PurePath):
        return _schemas.instance_of(annotation)
    raise TypeError(
        f"Cannot infer a schema for field {name!r}: {annotation!r}",
    )


def _has_nested_schema_metadata(
    annotation: Any,
    name: str,
    context: _DataclassAnnotation | None = None,
) -> bool:
    context, metadata = _resolve_annotation(
        _DataclassAnnotation(annotation) if context is None
        else replace(context, annotation=annotation), name,
    )
    if metadata:
        return True
    annotation = context.annotation
    supertype = getattr(annotation, "__supertype__", None)
    if supertype is not None:
        return _has_nested_schema_metadata(supertype, name, context)
    if typing.get_origin(annotation) is typing.Literal:
        return False
    return builtins.any(
        _has_nested_schema_metadata(item, name, context)
        for item in typing.get_args(annotation)
        if item is not Ellipsis
    )


def dataclass_field_schema(
    annotation: Any,
    name: str,
    *,
    stack: builtins.tuple[type[Any], ...],
    bindings: Mapping[typing.TypeVar, _DataclassAnnotation] | None = None,
    decoded_dataclasses: bool = False,
) -> builtins.tuple[_schemas.Schema[Any, Any], Any, bool]:
    """Return the field schema, resolved annotation, and explicit-schema flag."""
    context = _DataclassAnnotation(
        annotation,
        bindings={} if bindings is None else bindings,
    )
    context, metadata = _resolve_annotation(context, name)
    if metadata:
        schema = metadata[0]
    else:
        schema = infer_dataclass_field(
            context.annotation,
            name,
            stack=stack,
            context=context,
            decoded_dataclasses=decoded_dataclasses,
        )
    explicit = bool(metadata) or _has_nested_schema_metadata(
        context.annotation, name, context,
    )
    return schema, context.annotation, explicit
