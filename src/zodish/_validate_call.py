"""Function-signature validation built on native argument schemas."""

from __future__ import annotations

import functools
import inspect
import typing
from collections.abc import Callable
from typing import Any, cast

from . import schemas as _schemas
from ._core import Schema


def validate_call[F: Callable[..., Any]](fn: F) -> F:
    """Validate annotated arguments and the annotated return value."""
    signature = inspect.signature(fn)
    hints = typing.get_type_hints(fn, include_extras=True)
    argument_schemas: dict[str, Schema[Any, Any]] = {}
    for name, parameter in signature.parameters.items():
        annotation = hints.get(name, Any)
        schema: Schema[Any, Any] = _schemas.adapter(annotation)
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            schema = _schemas.tuple().rest(schema)
        elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
            schema = _schemas.record(schema)
        argument_schemas[name] = schema
    arguments_schema = _schemas.object(argument_schemas)
    return_schema: Schema[Any, Any] | None = (
        None if signature.return_annotation is inspect.Signature.empty
        else _schemas.adapter(hints.get("return", Any))
    )

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            bound.arguments = await arguments_schema.parse_async(bound.arguments)
            result = await fn(*bound.args, **bound.kwargs)
            if return_schema is not None:
                return await return_schema.parse_async(result)
            return result

        return cast(F, async_wrapper)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        bound.arguments = arguments_schema.parse(bound.arguments)
        result = fn(*bound.args, **bound.kwargs)
        if return_schema is not None:
            return return_schema.parse(result)
        return result

    return cast(F, wrapper)
