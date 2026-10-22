"""Codec schemas and reverse-graph encoding.

Imported at the end of ``zodish.schemas`` so the public module can re-export
``CodecSchema``, ``codec``, ``codec_async``, and ``codecs`` without a
module-load cycle.
"""

from __future__ import annotations

import annotationlib
import builtins
import dataclasses
import datetime as _datetime
import decimal as _decimal
import inspect
import json as _json
import pathlib as _pathlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast

from ._core import (
    _UNSET,
    MISSING,
    Missing,
    Schema,
    _async_callback,
    _brand_argument,
    _field_wire,
    _message,
    _sync_callback,
)
from ._errors import (
    ErrorMap,
    Failure,
    Issue,
    Success,
    ValidationError,
    _prefix_issue,
)
from ._native import NativeSchema
from .schemas import (
    _BASE64_PATTERN,
    _BASE64URL_PATTERN,
    _CONSTRAINT_KINDS,
    _DECIMAL_PATTERN,
    ArraySchema,
    DiscriminatedUnionSchema,
    IntersectionSchema,
    KeyedRecordSchema,
    MapSchema,
    ObjectSchema,
    RecordSchema,
    SetSchema,
    TupleSchema,
    UnionSchema,
    _encode_base64,
    _format_iso_duration,
    _parse_base64,
    _parse_decimal,
    _parse_iso_date,
    _parse_iso_datetime,
    _parse_iso_duration,
    _parse_iso_time,
    _parse_path,
    array,
    bytes,
    date,
    datetime,
    decimal,
    instance_of,
    json,
    lazy,
    object,
    path,
    record,
    string,
    time,
    timedelta,
    tuple,
    union,
    unknown,
)


@dataclass(frozen=True)
class CodecSchema[Out, In = Out](Schema[Out, In]):
    """Bidirectional conversion between an encoded and a decoded schema."""

    _encode_node: NativeSchema = field(repr=False, kw_only=True)
    _input_schema: Schema[In, Any] = field(
        repr=False, kw_only=True, compare=False,
    )
    _output_schema: Schema[Out, Any] = field(
        repr=False, kw_only=True, compare=False,
    )

    @property
    def input_schema(self) -> Schema[In, Any]:
        return self._input_schema

    @property
    def output_schema(self) -> Schema[Out, Any]:
        return self._output_schema

    def brand[Brand](
        self, newtype: Callable[[Out], Brand],
    ) -> CodecSchema[Brand, In]:
        _brand_argument(newtype)
        return CodecSchema[Brand, In](
            self._node,
            _encode_node=self._encode_node,
            _input_schema=self._input_schema,
            _output_schema=cast(Schema[Brand, Any], self._output_schema),
            _inner=self,
            _meta=self._meta,
        )

    def decode(
        self, value: In | Missing = MISSING, *, error: ErrorMap | None = None,
    ) -> Out:
        """Decode an encoded value; equivalent to ``parse``."""
        return self.parse(value, error=error)

    def safe_decode(
        self, value: In | Missing = MISSING, *, error: ErrorMap | None = None,
    ) -> Success[Out] | Failure:
        return self.safe_parse(value, error=error)

    async def decode_async(
        self, value: In | Missing = MISSING, *, error: ErrorMap | None = None,
    ) -> Out:
        """Decode, awaiting asynchronous codec and schema callbacks."""
        return await self.parse_async(value, error=error)

    async def safe_decode_async(
        self, value: In | Missing = MISSING, *, error: ErrorMap | None = None,
    ) -> Success[Out] | Failure:
        """Catch validation errors from asynchronous decoding only."""
        return await self.safe_parse_async(value, error=error)

    def optional(self) -> CodecSchema[Out | Missing, In | Missing]:
        result = _optional_codec(self)
        return cast(
            CodecSchema[Out | Missing, In | Missing],
            replace(result, _meta=self._meta),
        )

    def nonoptional(self) -> CodecSchema[Out, In]:
        result = _nonoptional_codec(self)
        return cast(CodecSchema[Out, In], replace(result, _meta=self._meta))

    def nullable(self) -> CodecSchema[Out | None, In | None]:
        result = _nullable_codec(self)
        return cast(
            CodecSchema[Out | None, In | None],
            replace(result, _meta=self._meta),
        )

    def nullish(self) -> CodecSchema[Out | Missing | None, In | Missing | None]:
        return self.nullable().optional()

    def record(self) -> CodecSchema[dict[str, Out], dict[str, In]]:
        result = _record_codec(self)
        return cast(CodecSchema[dict[str, Out], dict[str, In]], result)


@dataclass(frozen=True)
class AsyncCodecSchema[Out, In = Out](CodecSchema[Out, In]):
    """Codec whose decode and/or encode graph contains async callbacks."""

    _json_output_schema: Schema[Any, Any] = field(
        repr=False, kw_only=True, compare=False,
    )

    def brand[Brand](
        self, newtype: Callable[[Out], Brand],
    ) -> AsyncCodecSchema[Brand, In]:
        _brand_argument(newtype)
        return AsyncCodecSchema[Brand, In](
            self._node,
            _encode_node=self._encode_node,
            _input_schema=self._input_schema,
            _output_schema=cast(Schema[Brand, Any], self._output_schema),
            _json_output_schema=self._json_output_schema,
            _inner=self,
            _meta=self._meta,
        )

    def optional(self) -> AsyncCodecSchema[Out | Missing, In | Missing]:
        output_schema = self.output_schema.optional()
        json_output_schema = _json_output_schema_for(self).optional()
        return AsyncCodecSchema[Out | Missing, In | Missing](
            NativeSchema("optional", (self._node,), ()),
            _encode_node=NativeSchema("optional", (self._encode_node,), ()),
            _input_schema=self.input_schema.optional(),
            _output_schema=output_schema,
            _json_output_schema=json_output_schema,
            _inner=output_schema,
            _meta=self._meta,
        )

    def nonoptional(self) -> AsyncCodecSchema[Out, In]:
        output_schema = self.output_schema.nonoptional()
        json_output_schema = _json_output_schema_for(self).nonoptional()
        return AsyncCodecSchema[Out, In](
            NativeSchema("nonoptional", (self._node,), ()),
            _encode_node=NativeSchema("nonoptional", (self._encode_node,), ()),
            _input_schema=self.input_schema.nonoptional(),
            _output_schema=output_schema,
            _json_output_schema=json_output_schema,
            _inner=output_schema,
            _meta=self._meta,
        )

    def nullable(self) -> AsyncCodecSchema[Out | None, In | None]:
        output_schema = self.output_schema.nullable()
        json_output_schema = _json_output_schema_for(self).nullable()
        return AsyncCodecSchema[Out | None, In | None](
            NativeSchema("nullable", (self._node,), ()),
            _encode_node=NativeSchema("nullable", (self._encode_node,), ()),
            _input_schema=self.input_schema.nullable(),
            _output_schema=output_schema,
            _json_output_schema=json_output_schema,
            _inner=output_schema,
            _meta=self._meta,
        )

    def nullish(
        self,
    ) -> AsyncCodecSchema[Out | Missing | None, In | Missing | None]:
        return self.nullable().optional()

    def record(self) -> AsyncCodecSchema[dict[str, Out], dict[str, In]]:
        output_schema = record(self.output_schema)
        json_output_schema = record(_json_output_schema_for(self))
        return AsyncCodecSchema[dict[str, Out], dict[str, In]](
            NativeSchema("record", (self._node,), (None,)),
            _encode_node=NativeSchema(
                "record", (self._encode_node,), (None,),
            ),
            _input_schema=record(self.input_schema),
            _output_schema=output_schema,
            _json_output_schema=json_output_schema,
            _inner=output_schema,
        )


def _is_async_codec(codec: CodecSchema[Any, Any]) -> bool:
    return codec._node.has_async or codec._encode_node.has_async


def _promote_async_codec(
    codec: CodecSchema[Any, Any],
) -> AsyncCodecSchema[Any, Any]:
    if isinstance(codec, AsyncCodecSchema):
        return codec
    return AsyncCodecSchema[Any, Any](
        codec._node,
        _encode_node=codec._encode_node,
        _input_schema=codec.input_schema,
        _output_schema=codec.output_schema,
        _json_output_schema=_json_output_schema_for(codec),
        _inner=codec._inner,
        _meta=codec._meta,
    )


def _async_array_codec(
    inner: CodecSchema[Any, Any],
) -> AsyncCodecSchema[Any, Any]:
    output_schema = array(inner.output_schema)
    # Collection constructors start with empty metadata.
    return AsyncCodecSchema[Any, Any](
        NativeSchema("array", (inner._node,), ()),
        _encode_node=NativeSchema("array", (inner._encode_node,), ()),
        _input_schema=array(inner.input_schema),
        _output_schema=output_schema,
        _json_output_schema=array(_json_output_schema_for(inner)),
        _inner=output_schema,
    )


def _async_union_codec(
    branches: Sequence[CodecSchema[Any, Any]],
) -> AsyncCodecSchema[Any, Any]:
    output_schema = union(*(branch.output_schema for branch in branches))
    # Composition constructors start with empty metadata; no single branch owns
    # the container's title or description.
    return AsyncCodecSchema[Any, Any](
        NativeSchema(
            "union",
            builtins.tuple(branch._node for branch in branches),
            (),
        ),
        _encode_node=NativeSchema(
            "union",
            builtins.tuple(branch._encode_node for branch in branches),
            (),
        ),
        _input_schema=union(*(branch.input_schema for branch in branches)),
        _output_schema=output_schema,
        _json_output_schema=union(
            *(_json_output_schema_for(branch) for branch in branches)
        ),
        _inner=output_schema,
    )


def _async_tuple_codec(
    branches: Sequence[CodecSchema[Any, Any]],
) -> AsyncCodecSchema[Any, Any]:
    output_schema = tuple(*(branch.output_schema for branch in branches))
    # Composition constructors start with empty metadata; no single branch owns
    # the container's title or description.
    return AsyncCodecSchema[Any, Any](
        NativeSchema(
            "tuple",
            builtins.tuple(branch._node for branch in branches),
            (None,),
        ),
        _encode_node=NativeSchema(
            "tuple",
            builtins.tuple(branch._encode_node for branch in branches),
            (None,),
        ),
        _input_schema=tuple(*(branch.input_schema for branch in branches)),
        _output_schema=output_schema,
        _json_output_schema=tuple(
            *(_json_output_schema_for(branch) for branch in branches)
        ),
        _inner=output_schema,
    )


@dataclass(frozen=True)
class _CompoundCodecSchema[Out, In = Out](CodecSchema[Out, In]):
    _json_output_schema: Schema[Any, Any] = field(
        repr=False, kw_only=True, compare=False,
    )


@dataclass(frozen=True)
class _JsonTextSchema(Schema[str, str]):
    """String schema that validates JSON syntax with path-aware issues."""


def _json_text_input(message: str) -> _JsonTextSchema:
    def check(value: str) -> Issue | None:
        try:
            _json.loads(value)
        except _json.JSONDecodeError as error:
            return Issue(
                "invalid_format",
                message,
                params={"format": "json", "position": error.pos},
            )
        return None

    return _JsonTextSchema(
        NativeSchema(
            "super_refine",
            (NativeSchema("string", (), ()),),
            (check,),
        ),
    )


def codec[Out, In](
    input_schema: Schema[In, Any],
    output_schema: Schema[Out, Any],
    *,
    decode: Callable[[In], Out],
    encode: Callable[[Out], In],
) -> CodecSchema[Out, In]:
    """Build a bidirectional codec between encoded and decoded schemas.

    ``decode`` maps validated encoded input to decoded output; ``encode`` maps
    validated decoded input back to encoded output. Both directions validate
    their result through the corresponding schema.
    """
    if not isinstance(input_schema, Schema) or not isinstance(output_schema, Schema):
        raise TypeError("Codec input and output must be schemas")
    if not callable(decode):
        raise TypeError("decode must be callable")
    if not callable(encode):
        raise TypeError("encode must be callable")
    decode = _sync_callback(decode, "decode callback")
    encode = _sync_callback(encode, "encode callback")

    decode_node = NativeSchema(
        "pipe",
        (
            NativeSchema("transform", (input_schema._node,), (decode,)),
            output_schema._node,
        ),
        (),
    )
    encode_node = NativeSchema(
        "pipe",
        (
            NativeSchema("transform", (output_schema._node,), (encode,)),
            input_schema._node,
        ),
        (),
    )
    return CodecSchema[Out, In](
        decode_node,
        _encode_node=encode_node,
        _input_schema=input_schema,
        _output_schema=output_schema,
        _inner=output_schema,
    )


def codec_async[Out, In](
    input_schema: Schema[In, Any],
    output_schema: Schema[Out, Any],
    *,
    decode: Callable[[In], Awaitable[Out]],
    encode: Callable[[Out], Awaitable[In]],
) -> AsyncCodecSchema[Out, In]:
    """Build a bidirectional codec with asynchronous callbacks."""
    if not isinstance(input_schema, Schema) or not isinstance(output_schema, Schema):
        raise TypeError("Codec input and output must be schemas")
    decode = _async_callback(decode, "decode_async callback")
    encode = _async_callback(encode, "encode_async callback")

    decode_node = NativeSchema(
        "pipe",
        (
            NativeSchema("async_transform", (input_schema._node,), (decode,)),
            output_schema._node,
        ),
        (),
    )
    encode_node = NativeSchema(
        "pipe",
        (
            NativeSchema("async_transform", (output_schema._node,), (encode,)),
            input_schema._node,
        ),
        (),
    )
    return AsyncCodecSchema[Out, In](
        decode_node,
        _encode_node=encode_node,
        _input_schema=input_schema,
        _output_schema=output_schema,
        _json_output_schema=output_schema,
        _inner=output_schema,
    )


def _json_output_schema_for(schema: Schema[Any, Any]) -> Schema[Any, Any]:
    if isinstance(schema, AsyncCodecSchema):
        return schema._json_output_schema
    if isinstance(schema, _CompoundCodecSchema):
        return schema._json_output_schema
    if isinstance(schema, CodecSchema):
        return schema.output_schema
    return schema


def _replace_json_class_ref(
    schema: Schema[Any, Any],
    cls: type[Any],
    replacement: Schema[Any, Any],
) -> Schema[Any, Any]:
    """Replace ``instance_of(cls)`` with a recursive JSON ``$ref`` schema."""
    node = schema._node
    if (
        node.kind == "instance"
        and node.args
        and node.args[0] is cls
    ):
        return replacement
    if isinstance(schema, ArraySchema):
        item = _replace_json_class_ref(schema.item, cls, replacement)
        return schema if item is schema.item else replace(schema, item=item)
    if isinstance(schema, SetSchema):
        item = _replace_json_class_ref(schema.item, cls, replacement)
        return schema if item is schema.item else replace(schema, item=item)
    if isinstance(schema, RecordSchema):
        value = _replace_json_class_ref(
            schema.value_schema, cls, replacement,
        )
        return (
            schema
            if value is schema.value_schema
            else replace(schema, value_schema=value)
        )
    if isinstance(schema, KeyedRecordSchema):
        key = _replace_json_class_ref(schema.key_schema, cls, replacement)
        value = _replace_json_class_ref(
            schema.value_schema, cls, replacement,
        )
        return (
            schema
            if key is schema.key_schema and value is schema.value_schema
            else replace(schema, key_schema=key, value_schema=value)
        )
    if isinstance(schema, MapSchema):
        key = _replace_json_class_ref(schema.key_schema, cls, replacement)
        value = _replace_json_class_ref(
            schema.value_schema, cls, replacement,
        )
        return (
            schema
            if key is schema.key_schema and value is schema.value_schema
            else replace(schema, key_schema=key, value_schema=value)
        )
    if isinstance(schema, TupleSchema):
        items = builtins.tuple(
            _replace_json_class_ref(item, cls, replacement)
            for item in schema.items
        )
        rest = (
            None
            if schema._rest is None
            else _replace_json_class_ref(schema._rest, cls, replacement)
        )
        if all(
            new is old
            for new, old in zip(items, schema.items, strict=True)
        ) and rest is schema._rest:
            return schema
        return replace(schema, items=items, _rest=rest)
    if isinstance(schema, UnionSchema):
        options = builtins.tuple(
            _replace_json_class_ref(option, cls, replacement)
            for option in schema.options
        )
        if all(
            new is old
            for new, old in zip(options, schema.options, strict=True)
        ):
            return schema
        return replace(schema, options=options)
    if isinstance(schema, ObjectSchema):
        shape = {
            key: _replace_json_class_ref(value, cls, replacement)
            for key, value in schema.shape.items()
        }
        catchall = (
            None
            if schema._catchall is None
            else _replace_json_class_ref(
                schema._catchall, cls, replacement,
            )
        )
        if (
            all(
                shape[key] is schema.shape[key]
                for key in shape
            )
            and catchall is schema._catchall
        ):
            return schema
        return replace(schema, shape=shape, _catchall=catchall)
    if isinstance(schema, DiscriminatedUnionSchema):
        option_map = {
            key: _replace_json_class_ref(value, cls, replacement)
            for key, value in schema.options.items()
        }
        if all(
            option_map[key] is schema.options[key]
            for key in option_map
        ):
            return schema
        return replace(schema, options=option_map)
    if isinstance(schema, IntersectionSchema):
        left = _replace_json_class_ref(schema.left, cls, replacement)
        right = _replace_json_class_ref(schema.right, cls, replacement)
        return (
            schema
            if left is schema.left and right is schema.right
            else replace(schema, left=left, right=right)
        )
    if schema._inner is not None:
        inner = _replace_json_class_ref(schema._inner, cls, replacement)
        return schema if inner is schema._inner else replace(schema, _inner=inner)
    return schema


def _node_has_async_without_lazy(node: NativeSchema) -> bool:
    """Detect async callbacks while ignoring lazy recursion placeholders."""
    if node.kind == "lazy":
        return False
    if node.kind.startswith("async_"):
        return True
    if builtins.any(
        _node_has_async_without_lazy(child) for child in node.children
    ):
        return True
    for arg in node.args:
        if isinstance(arg, NativeSchema):
            if _node_has_async_without_lazy(arg):
                return True
        elif isinstance(arg, (builtins.tuple, builtins.list)):
            if builtins.any(
                isinstance(item, NativeSchema)
                and _node_has_async_without_lazy(item)
                for item in arg
            ):
                return True
        elif isinstance(arg, Mapping) and builtins.any(
            isinstance(item, NativeSchema)
            and _node_has_async_without_lazy(item)
            for item in arg.values()
        ):
            return True
    return False


def _identity_codec(schema: Schema[Any, Any]) -> CodecSchema[Any, Any]:
    return codec(
        schema,
        schema,
        decode=lambda value: value,
        encode=lambda value: value,
    )


def _codec_transform(
    input_schema: Schema[Any, Any],
    callback: Callable[[Any], Any],
) -> NativeSchema:
    """Validate only the container shape; inner codecs validate their items."""
    return NativeSchema("transform", (input_schema._node,), (callback,))


def _array_codec(inner: CodecSchema[Any, Any]) -> CodecSchema[Any, Any]:
    if _is_async_codec(inner):
        return _async_array_codec(_promote_async_codec(inner))

    def decode_values(values: list[Any]) -> list[Any]:
        result: list[Any] = []
        issues: list[Issue] = []
        for index, value in enumerate(values):
            try:
                result.append(inner.decode(value))
            except ValidationError as error:
                issues.extend(_prefix_issue(item, index) for item in error.issues)
        if issues:
            raise ValidationError(issues)
        return result

    def encode_values(values: list[Any]) -> list[Any]:
        result: list[Any] = []
        issues: list[Issue] = []
        for index, value in enumerate(values):
            try:
                result.append(inner.encode(value))
            except ValidationError as error:
                issues.extend(_prefix_issue(item, index) for item in error.issues)
        if issues:
            raise ValidationError(issues)
        return result

    output_schema = array(inner.output_schema)
    placeholder = array(unknown())
    # Collection constructors start with empty metadata.
    return _CompoundCodecSchema[Any, Any](
        _codec_transform(placeholder, decode_values),
        _encode_node=_codec_transform(placeholder, encode_values),
        _input_schema=array(inner.input_schema),
        _output_schema=output_schema,
        _json_output_schema=array(_json_output_schema_for(inner)),
        _inner=output_schema,
    )


def _record_codec(inner: CodecSchema[Any, Any]) -> CodecSchema[Any, Any]:
    if _is_async_codec(inner):
        return _promote_async_codec(inner).record()

    def decode_values(values: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        issues: list[Issue] = []
        for key, value in values.items():
            try:
                result[key] = inner.decode(value)
            except ValidationError as error:
                issues.extend(_prefix_issue(item, key) for item in error.issues)
        if issues:
            raise ValidationError(issues)
        return result

    def encode_values(values: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        issues: list[Issue] = []
        for key, value in values.items():
            try:
                result[key] = inner.encode(value)
            except ValidationError as error:
                issues.extend(_prefix_issue(item, key) for item in error.issues)
        if issues:
            raise ValidationError(issues)
        return result

    output_schema = record(inner.output_schema)
    placeholder = record(unknown())
    # Collection constructors start with empty metadata.
    return _CompoundCodecSchema[Any, Any](
        _codec_transform(placeholder, decode_values),
        _encode_node=_codec_transform(placeholder, encode_values),
        _input_schema=record(inner.input_schema),
        _output_schema=output_schema,
        _json_output_schema=record(_json_output_schema_for(inner)),
        _inner=output_schema,
    )


def _optional_codec(inner: CodecSchema[Any, Any]) -> CodecSchema[Any, Any]:
    if _is_async_codec(inner):
        return _promote_async_codec(inner).optional()

    def decode_value(value: Any) -> Any:
        return MISSING if value is MISSING else inner.decode(value)

    def encode_value(value: Any) -> Any:
        return MISSING if value is MISSING else inner.encode(value)

    output_schema = inner.output_schema.optional()
    placeholder = unknown().optional()
    return _CompoundCodecSchema[Any, Any](
        _codec_transform(placeholder, decode_value),
        _encode_node=_codec_transform(placeholder, encode_value),
        _input_schema=inner.input_schema.optional(),
        _output_schema=output_schema,
        _json_output_schema=_json_output_schema_for(inner).optional(),
        _inner=output_schema,
        _meta=inner._meta,
    )


def _nonoptional_codec(inner: CodecSchema[Any, Any]) -> CodecSchema[Any, Any]:
    if _is_async_codec(inner):
        return _promote_async_codec(inner).nonoptional()
    output_schema = inner.output_schema.nonoptional()
    placeholder = unknown().nonoptional()
    return _CompoundCodecSchema[Any, Any](
        _codec_transform(placeholder, inner.decode),
        _encode_node=_codec_transform(placeholder, inner.encode),
        _input_schema=inner.input_schema.nonoptional(),
        _output_schema=output_schema,
        _json_output_schema=_json_output_schema_for(inner).nonoptional(),
        _inner=output_schema,
        _meta=inner._meta,
    )


def _nullable_codec(inner: CodecSchema[Any, Any]) -> CodecSchema[Any, Any]:
    if _is_async_codec(inner):
        return _promote_async_codec(inner).nullable()

    def decode_value(value: Any) -> Any:
        return None if value is None else inner.decode(value)

    def encode_value(value: Any) -> Any:
        return None if value is None else inner.encode(value)

    output_schema = inner.output_schema.nullable()
    placeholder = unknown().nullable()
    return _CompoundCodecSchema[Any, Any](
        _codec_transform(placeholder, decode_value),
        _encode_node=_codec_transform(placeholder, encode_value),
        _input_schema=inner.input_schema.nullable(),
        _output_schema=output_schema,
        _json_output_schema=_json_output_schema_for(inner).nullable(),
        _inner=output_schema,
        _meta=inner._meta,
    )


def _union_codec(options: Sequence[Schema[Any, Any]]) -> CodecSchema[Any, Any]:
    if not options:
        raise ValueError("Union codec requires at least one schema")
    if len(options) == 1:
        option = options[0]
        return option if isinstance(option, CodecSchema) else _identity_codec(option)
    branches = [
        option if isinstance(option, CodecSchema) else _identity_codec(option)
        for option in options
    ]
    if any(_is_async_codec(branch) for branch in branches):
        return _async_union_codec(branches)

    def decode_value(value: Any) -> Any:
        branch_issues: list[builtins.tuple[Issue, ...]] = []
        for branch in branches:
            try:
                return branch.decode(value)
            except ValidationError as error:
                branch_issues.append(error.issues)
        raise ValidationError([
            Issue(
                "invalid_union",
                "Invalid input",
                branches=builtins.tuple(branch_issues),
            ),
        ])

    def encode_value(value: Any) -> Any:
        branch_issues: list[builtins.tuple[Issue, ...]] = []
        for branch in branches:
            try:
                return branch.encode(value)
            except ValidationError as error:
                branch_issues.append(error.issues)
        raise ValidationError([
            Issue(
                "invalid_union",
                "Invalid input",
                branches=builtins.tuple(branch_issues),
            ),
        ])

    output_schema = union(*(branch.output_schema for branch in branches))
    placeholder = unknown().optional()
    # Composition constructors start with empty metadata; no single branch owns
    # the container's title or description.
    return _CompoundCodecSchema[Any, Any](
        _codec_transform(placeholder, decode_value),
        _encode_node=_codec_transform(placeholder, encode_value),
        _input_schema=union(*(branch.input_schema for branch in branches)),
        _output_schema=output_schema,
        _json_output_schema=union(
            *(_json_output_schema_for(option) for option in options)
        ),
        _inner=output_schema,
    )


def _tuple_codec(options: Sequence[Schema[Any, Any]]) -> CodecSchema[Any, Any]:
    branches = [
        option if isinstance(option, CodecSchema) else _identity_codec(option)
        for option in options
    ]
    if any(_is_async_codec(branch) for branch in branches):
        return _async_tuple_codec(branches)

    def decode_values(values: builtins.tuple[Any, ...]) -> builtins.tuple[Any, ...]:
        result: list[Any] = []
        issues: list[Issue] = []
        for index, (branch, value) in enumerate(
            zip(branches, values, strict=True),
        ):
            try:
                result.append(branch.decode(value))
            except ValidationError as error:
                issues.extend(_prefix_issue(item, index) for item in error.issues)
        if issues:
            raise ValidationError(issues)
        return builtins.tuple(result)

    def encode_values(values: builtins.tuple[Any, ...]) -> builtins.tuple[Any, ...]:
        result: list[Any] = []
        issues: list[Issue] = []
        for index, (branch, value) in enumerate(
            zip(branches, values, strict=True),
        ):
            try:
                result.append(branch.encode(value))
            except ValidationError as error:
                issues.extend(_prefix_issue(item, index) for item in error.issues)
        if issues:
            raise ValidationError(issues)
        return builtins.tuple(result)

    output_schema = tuple(*(branch.output_schema for branch in branches))
    placeholder = tuple(*(unknown() for _ in branches))
    # Composition constructors start with empty metadata; no single branch owns
    # the container's title or description.
    return _CompoundCodecSchema[Any, Any](
        _codec_transform(placeholder, decode_values),
        _encode_node=_codec_transform(placeholder, encode_values),
        _input_schema=tuple(*(branch.input_schema for branch in branches)),
        _output_schema=output_schema,
        _json_output_schema=tuple(
            *(_json_output_schema_for(option) for option in options)
        ),
        _inner=output_schema,
    )


_decimal_output = decimal
_path_output = path


class _Codecs:
    @staticmethod
    def iso_date(
        *, message: str = "Invalid date",
    ) -> CodecSchema[_datetime.date, str]:
        validated = _message(message, "Invalid date")
        return codec(
            string().date(message=validated),
            date(),
            decode=lambda value: _parse_iso_date(value, validated),
            encode=lambda value: value.isoformat(),
        )

    @staticmethod
    def iso_time(
        *, message: str = "Invalid time",
    ) -> CodecSchema[_datetime.time, str]:
        validated = _message(message, "Invalid time")
        return codec(
            string().time(message=validated),
            time(),
            decode=lambda value: _parse_iso_time(value, validated),
            encode=lambda value: value.isoformat(),
        )

    @staticmethod
    def iso_datetime(
        *, message: str = "Invalid datetime",
    ) -> CodecSchema[_datetime.datetime, str]:
        validated = _message(message, "Invalid datetime")
        return codec(
            string().datetime(message=validated),
            datetime(),
            decode=lambda value: _parse_iso_datetime(value, validated),
            encode=lambda value: value.isoformat(),
        )

    @staticmethod
    def iso_duration(
        *, message: str = "Invalid duration",
    ) -> CodecSchema[_datetime.timedelta, str]:
        validated = _message(message, "Invalid duration")
        return codec(
            string().duration(message=validated),
            timedelta(),
            decode=lambda value: _parse_iso_duration(value, validated),
            encode=_format_iso_duration,
        )

    @staticmethod
    def decimal(
        *, message: str = "Invalid decimal string",
    ) -> CodecSchema[_decimal.Decimal, str]:
        validated = _message(message, "Invalid decimal string")
        return codec(
            string()._regex(_DECIMAL_PATTERN, validated, "decimal"),
            _decimal_output(),
            decode=lambda value: _parse_decimal(value, validated),
            encode=lambda value: str(value),
        )

    @staticmethod
    def json_text(*, message: str = "Invalid JSON") -> CodecSchema[Any, str]:
        validated = _message(message, "Invalid JSON")
        return codec(
            _json_text_input(validated),
            json(),
            decode=lambda value: _json.loads(value),
            encode=lambda value: _json.dumps(value),
        )

    @staticmethod
    def path() -> CodecSchema[_pathlib.PurePath, str]:
        return codec(
            string(),
            _path_output(),
            decode=_parse_path,
            encode=lambda value: str(value),
        )

    @staticmethod
    def bytes_base64(
        *,
        urlsafe: bool = False,
        message: str = "Invalid base64 string",
    ) -> CodecSchema[builtins.bytes, str]:
        if not isinstance(urlsafe, bool):
            raise TypeError("urlsafe must be a boolean")
        validated = _message(message, "Invalid base64 string")
        pattern = _BASE64URL_PATTERN if urlsafe else _BASE64_PATTERN
        format_name = "base64url" if urlsafe else "base64"
        return codec(
            string()._regex(pattern, validated, format_name),
            bytes(),
            decode=lambda value: _parse_base64(value, urlsafe, validated),
            encode=lambda value: _encode_base64(value, urlsafe),
        )

    @staticmethod
    def array[Out, In](
        codec: CodecSchema[Out, In],
    ) -> CodecSchema[list[Out], list[In]]:
        if not isinstance(codec, CodecSchema):
            raise TypeError("array expects a codec")
        result = _array_codec(codec)
        return cast(CodecSchema[list[Out], list[In]], result)

    @staticmethod
    def union(
        *options: Schema[Any, Any],
    ) -> CodecSchema[Any, Any]:
        if not all(isinstance(item, Schema) for item in options):
            raise TypeError("union expects schemas")
        return _union_codec(options)

    @staticmethod
    def tuple(
        *options: Schema[Any, Any],
    ) -> CodecSchema[Any, Any]:
        if not all(isinstance(item, Schema) for item in options):
            raise TypeError("tuple expects schemas")
        return _tuple_codec(options)

    @staticmethod
    def dataclass[DataclassT](
        cls: type[DataclassT],
        *,
        fields: Mapping[str, Schema[Any, Any]] | None = None,
        _stack: builtins.tuple[type[Any], ...] = (),
    ) -> CodecSchema[DataclassT, dict[str, Any]]:
        """Build an encoded-dictionary ↔ dataclass codec.

        Missing field schemas are inferred from type annotations. Explicit
        entries override inference, including ``Annotated[T, schema]`` metadata.
        Type aliases with ordinary type parameters and defaults are resolved
        recursively. Plain schemas act as validators; codec field schemas supply
        the encoded and decoded forms. Fields with ``init=False`` are ignored.
        """
        # Inference uses the schema builders, so import it after they exist.
        from ._annotations import (
            _DataclassAnnotation,
            dataclass_type_hints,
            infer_dataclass_field,
        )

        if not isinstance(cls, type) or not dataclasses.is_dataclass(cls):
            raise TypeError("dataclass codec expects a dataclass type")
        if cls in _stack:
            raise TypeError(
                "Recursive dataclass annotations are not supported",
            )
        stack = (*_stack, cls)
        if fields is not None and not isinstance(fields, Mapping):
            raise TypeError("Dataclass fields must be a mapping")
        from ._validated_dataclasses import (
            _initvar_annotation,
            _is_initvar_field,
            assemble_validated,
            default_value,
            marker_for,
        )

        marker = marker_for(cls)
        fields_by_name: Mapping[str, dataclasses.Field[Any]]
        has_default: Mapping[str, bool]
        if marker is None:
            for base in cls.__mro__[1:]:
                if marker_for(base) is not None:
                    raise TypeError(
                        "Dataclass codecs do not support subclasses of "
                        "validated dataclasses",
                    )
            init_fields = [item for item in dataclasses.fields(cls) if item.init]
            names = builtins.tuple(item.name for item in init_fields)
            stored_names = names
            initvar_names: builtins.tuple[str, ...] = ()
            non_init_names: builtins.tuple[str, ...] = ()
            fields_by_name = {item.name: item for item in init_fields}
            has_default = {
                name: (
                    fields_by_name[name].default is not dataclasses.MISSING
                    or fields_by_name[name].default_factory
                    is not dataclasses.MISSING
                )
                for name in names
            }
        else:
            names = marker.constructor_names
            stored_names = marker.stored_names
            initvar_names = marker.initvar_names
            non_init_names = marker.non_init_names
            fields_by_name = marker.fields
            has_default = marker.has_default
        expected = builtins.set(names)
        try:
            init_signature = inspect.signature(cls.__init__)
        except (NameError, TypeError):
            init_signature = inspect.signature(
                cls.__init__,
                annotation_format=annotationlib.Format.FORWARDREF,
            )
        parameters = {
            name
            for name, parameter in init_signature.parameters.items()
            if parameter.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        }
        parameters.discard("self")
        unsupported = parameters - expected
        if unsupported:
            raise TypeError(
                "Unsupported dataclass init parameters: "
                + ", ".join(sorted(unsupported))
            )
        missing_parameters = expected - parameters
        if missing_parameters:
            raise TypeError(
                "Unsupported dataclass init fields: "
                + ", ".join(sorted(missing_parameters))
            )

        overrides = {} if fields is None else dict(fields)
        provided = builtins.set(overrides)
        unknown_fields = provided - expected
        if unknown_fields:
            raise ValueError(
                "Unknown dataclass field schemas: "
                + ", ".join(sorted(builtins.map(repr, unknown_fields)))
            )
        missing = expected - provided
        hints = (
            (
                dict(marker.hints)
                if marker is not None
                else dataclass_type_hints(cls)
            )
            if missing
            else {}
        )

        recursion_holder: dict[str, Any] = {}
        json_holder: dict[str, Any] = {}
        lazy_output: Schema[Any, Any] | None = None
        recursive_codec: CodecSchema[Any, Any] | None = None

        def infer_schemas(
            recursive_schema: CodecSchema[Any, Any] | None,
        ) -> dict[str, Schema[Any, Any]]:
            result: dict[str, Schema[Any, Any]] = {}
            for name in names:
                if name in overrides:
                    schema = overrides[name]
                else:
                    annotation = hints.get(name, _UNSET)
                    if annotation is _UNSET:
                        raise TypeError(
                            f"Cannot infer a schema for field {name!r}",
                        )
                    if _is_initvar_field(fields_by_name[name]):
                        annotation = _initvar_annotation(annotation, name)
                    context = (
                        _DataclassAnnotation(
                            annotation, bindings=marker.type_bindings,
                        )
                        if marker is not None
                        else None
                    )
                    schema = infer_dataclass_field(
                        annotation,
                        name,
                        stack=stack,
                        context=context,
                        self_codec=recursive_schema,
                        self_type=cls if recursive_schema is not None else None,
                    )
                if not isinstance(schema, Schema):
                    raise TypeError(f"Field {name!r} must be a schema")
                result[name] = schema
            return result

        def make_recursive_codec(
            asynchronous: bool,
        ) -> CodecSchema[Any, Any]:
            assert lazy_output is not None
            output_placeholder = instance_of(cls)
            if asynchronous:
                async def decode_recursive_async(value: Any) -> Any:
                    return await recursion_holder["codec"].decode_async(value)

                async def encode_recursive_async(value: Any) -> Any:
                    return await recursion_holder["codec"].encode_async(value)

                return AsyncCodecSchema[Any, Any](
                    NativeSchema(
                        "pipe",
                        (
                            NativeSchema(
                                "async_transform",
                                (lazy_input._node,),
                                (decode_recursive_async,),
                            ),
                            output_placeholder._node,
                        ),
                        (),
                    ),
                    _encode_node=NativeSchema(
                        "pipe",
                        (
                            NativeSchema(
                                "async_transform",
                                (output_placeholder._node,),
                                (encode_recursive_async,),
                            ),
                            lazy_input._node,
                        ),
                        (),
                    ),
                    _input_schema=lazy_input,
                    _output_schema=output_placeholder,
                    _json_output_schema=lazy_output,
                    _inner=output_placeholder,
                )

            def decode_recursive(value: Any) -> Any:
                return recursion_holder["codec"].decode(value)

            def encode_recursive(value: Any) -> Any:
                return recursion_holder["codec"].encode(value)

            return _CompoundCodecSchema[Any, Any](
                NativeSchema(
                    "pipe",
                    (
                        NativeSchema(
                            "transform",
                            (lazy_input._node,),
                            (decode_recursive,),
                        ),
                        output_placeholder._node,
                    ),
                    (),
                ),
                _encode_node=NativeSchema(
                    "pipe",
                    (
                        NativeSchema(
                            "transform",
                            (output_placeholder._node,),
                            (encode_recursive,),
                        ),
                        lazy_input._node,
                    ),
                    (),
                ),
                _input_schema=lazy_input,
                _output_schema=output_placeholder,
                _json_output_schema=lazy_output,
                _inner=output_placeholder,
            )

        if marker is not None and marker.recursive:
            json_id = f"{cls.__module__}.{cls.__qualname__}"
            lazy_input = lazy(
                lambda: json_holder["input_schema"],
            ).meta({"id": json_id})
            lazy_output = lazy(
                lambda: json_holder["output_json_schema"],
            ).meta({"id": json_id})
            recursive_codec = make_recursive_codec(False)
            schemas_by_name = infer_schemas(recursive_codec)
            recursive_async_needed = (
                builtins.any(
                    _node_has_async_without_lazy(schema._node)
                    for schema in schemas_by_name.values()
                )
                or builtins.any(
                    _node_has_async_without_lazy(
                        schema._encode_node
                        if isinstance(schema, CodecSchema)
                        else schema._node,
                    )
                    for schema in (
                        schemas_by_name[name] for name in stored_names
                    )
                )
            )
            if recursive_async_needed:
                recursive_codec = make_recursive_codec(True)
                schemas_by_name = infer_schemas(recursive_codec)
        else:
            schemas_by_name = infer_schemas(None)

        def input_field_schema(name: str) -> Schema[Any, Any]:
            schema = schemas_by_name[name]
            base = (
                schema.input_schema
                if isinstance(schema, CodecSchema)
                else schema
            )
            if has_default[name]:
                return base.nonoptional().optional()
            if base._node.kind == "optional":
                raise ValueError(
                    f"Field {name!r} accepts missing input but the dataclass "
                    "field has no default"
                )
            return base

        input_schemas: dict[str, Schema[Any, Any]] = {
            name: input_field_schema(name) for name in names
        }
        input_schema = object(input_schemas)
        # Codec fields are validated and converted by the codec itself, so
        # their shape entries only enforce presence. Plain fields are validated
        # by their normal schemas.
        decode_shape: dict[str, Schema[Any, Any]] = {}
        for name in names:
            if isinstance(schemas_by_name[name], CodecSchema):
                decode_shape[name] = (
                    unknown().optional() if has_default[name] else unknown()
                )
            else:
                decode_shape[name] = input_schemas[name]
        decode_input_schema = object(decode_shape)
        output_schema: Schema[DataclassT, Any] = instance_of(cls)
        output_shape = {}
        for name in stored_names:
            field_output = _json_output_schema_for(schemas_by_name[name])
            if recursive_codec is not None and lazy_output is not None:
                field_output = _replace_json_class_ref(
                    field_output, cls, lazy_output,
                )
            output_shape[name] = (
                field_output.nonoptional()
                if has_default[name]
                else field_output
            )
        output_json_schema = object(output_shape)

        def decode_value(data: dict[str, Any]) -> DataclassT:
            decoded: dict[str, Any] = {}
            initvar_decoded: dict[str, Any] = {}
            issues: list[Issue] = []
            for name in names:
                schema = schemas_by_name[name]
                value = data.get(name, MISSING)
                if value is MISSING:
                    if not has_default[name]:
                        issues.append(Issue("required", "Required", (name,)))
                        continue
                    if marker is None:
                        continue
                    try:
                        value = marker.construction_schemas[name].parse(
                            default_value(fields_by_name[name]),
                        )
                    except ValidationError as error:
                        issues.extend(
                            _prefix_issue(item, name) for item in error.issues
                        )
                        continue
                    if value is MISSING:
                        raise TypeError(
                            f"Field {name!r} default produced MISSING",
                        )
                    if name in initvar_names:
                        initvar_decoded[name] = value
                    else:
                        decoded[name] = value
                    continue
                if isinstance(schema, CodecSchema):
                    try:
                        value = schema.decode(value)
                    except ValidationError as error:
                        issues.extend(
                            _prefix_issue(item, name) for item in error.issues
                        )
                        continue
                if marker is not None:
                    guard = marker.annotation_guards[name]
                    if guard is not None and (
                        name in overrides or marker.guards[name] is not None
                    ):
                        value = guard.parse(value)
                if marker is not None and name in initvar_names:
                    initvar_decoded[name] = value
                else:
                    decoded[name] = value
            if marker is not None:
                for name in non_init_names:
                    try:
                        value = marker.construction_schemas[name].parse(
                            default_value(fields_by_name[name]),
                        )
                    except ValidationError as error:
                        issues.extend(
                            _prefix_issue(item, name) for item in error.issues
                        )
                        continue
                    if value is MISSING:
                        raise TypeError(
                            f"Field {name!r} default produced MISSING",
                        )
                    decoded[name] = value
            if issues:
                raise ValidationError(issues)
            if marker is not None:
                initvar_values = [
                    initvar_decoded[name] for name in initvar_names
                ]
                return cast(
                    DataclassT,
                    assemble_validated(cls, decoded, initvar_values),
                )
            return cls(**decoded)

        async def decode_value_async(data: dict[str, Any]) -> DataclassT:
            decoded: dict[str, Any] = {}
            initvar_decoded: dict[str, Any] = {}
            issues: list[Issue] = []
            for name in names:
                schema = schemas_by_name[name]
                value = data.get(name, MISSING)
                if value is MISSING:
                    if not has_default[name]:
                        issues.append(Issue("required", "Required", (name,)))
                        continue
                    if marker is None:
                        continue
                    try:
                        value = marker.construction_schemas[name].parse(
                            default_value(fields_by_name[name]),
                        )
                    except ValidationError as error:
                        issues.extend(
                            _prefix_issue(item, name) for item in error.issues
                        )
                        continue
                    if value is MISSING:
                        raise TypeError(
                            f"Field {name!r} default produced MISSING",
                        )
                    if name in initvar_names:
                        initvar_decoded[name] = value
                    else:
                        decoded[name] = value
                    continue
                if isinstance(schema, CodecSchema):
                    try:
                        value = await schema.decode_async(value)
                    except ValidationError as error:
                        issues.extend(
                            _prefix_issue(item, name) for item in error.issues
                        )
                        continue
                if marker is not None:
                    guard = marker.annotation_guards[name]
                    if guard is not None and (
                        name in overrides or marker.guards[name] is not None
                    ):
                        value = guard.parse(value)
                if marker is not None and name in initvar_names:
                    initvar_decoded[name] = value
                else:
                    decoded[name] = value
            if marker is not None:
                for name in non_init_names:
                    try:
                        value = marker.construction_schemas[name].parse(
                            default_value(fields_by_name[name]),
                        )
                    except ValidationError as error:
                        issues.extend(
                            _prefix_issue(item, name) for item in error.issues
                        )
                        continue
                    if value is MISSING:
                        raise TypeError(
                            f"Field {name!r} default produced MISSING",
                        )
                    decoded[name] = value
            if issues:
                raise ValidationError(issues)
            if marker is not None:
                initvar_values = [
                    initvar_decoded[name] for name in initvar_names
                ]
                return cast(
                    DataclassT,
                    assemble_validated(cls, decoded, initvar_values),
                )
            return cls(**decoded)

        def encode_value(value: DataclassT) -> dict[str, Any]:
            encoded: dict[str, Any] = {}
            issues: list[Issue] = []
            for name in stored_names:
                schema = schemas_by_name[name]
                field_value = getattr(value, name)
                try:
                    if isinstance(schema, CodecSchema):
                        field_value = schema.encode(field_value)
                    else:
                        field_value = input_schemas[name].parse(field_value)
                except ValidationError as error:
                    issues.extend(
                        _prefix_issue(item, name) for item in error.issues
                    )
                    continue
                encoded[name] = field_value
            if issues:
                raise ValidationError(issues)
            return encoded

        async def encode_value_async(value: DataclassT) -> dict[str, Any]:
            encoded: dict[str, Any] = {}
            issues: list[Issue] = []
            for name in stored_names:
                schema = schemas_by_name[name]
                field_value = getattr(value, name)
                try:
                    if isinstance(schema, CodecSchema):
                        field_value = await schema.encode_async(field_value)
                    else:
                        field_value = await input_schemas[name].parse_async(
                            field_value,
                        )
                except ValidationError as error:
                    issues.extend(
                        _prefix_issue(item, name) for item in error.issues
                    )
                    continue
                encoded[name] = field_value
            if issues:
                raise ValidationError(issues)
            return encoded

        if recursive_codec is None:
            decode_async_needed = any(
                isinstance(schema, CodecSchema) and schema._node.has_async
                for schema in schemas_by_name.values()
            )
            encode_async_needed = any(
                (
                    schema._encode_node.has_async
                    if isinstance(schema, CodecSchema)
                    else schema._node.has_async
                )
                for schema in (
                    schemas_by_name[name] for name in stored_names
                )
            )
        else:
            decode_async_needed = any(
                _node_has_async_without_lazy(schema._node)
                for schema in schemas_by_name.values()
            )
            encode_async_needed = any(
                _node_has_async_without_lazy(
                    schema._encode_node
                    if isinstance(schema, CodecSchema)
                    else schema._node,
                )
                for schema in (
                    schemas_by_name[name] for name in stored_names
                )
            )
        if marker is not None:
            placeholder = unknown()._node
            decode_children: list[NativeSchema] = []
            field_flags: list[int] = []
            field_default_values: list[Any] = []
            field_default_factories: list[Any] = []
            input_names: list[str] = []
            for name in names:
                schema = schemas_by_name[name]
                supplied = (
                    schema._node
                    if isinstance(schema, CodecSchema)
                    else input_schemas[name]._node
                )
                input_names.append(
                    name
                    if isinstance(schema, CodecSchema)
                    else _field_wire(input_schemas[name]._node) or name
                )
                guard = marker.annotation_guards[name]
                if guard is not None and (
                    name in overrides or marker.guards[name] is not None
                ):
                    supplied = NativeSchema(
                        "output_guard",
                        (supplied, guard._node.children[1]),
                        guard._node.args,
                    )
                decode_children.append(supplied)
                decode_children.append(
                    marker.construction_schemas[name]._node
                    if has_default[name]
                    else placeholder
                )
                field_flags.append(
                    (1 | 4 if has_default[name] else 0)
                    | (2 if name in initvar_names else 0)
                )
                field = fields_by_name[name]
                field_default_values.append(
                    MISSING
                    if field.default is dataclasses.MISSING
                    else field.default
                )
                field_default_factories.append(
                    None
                    if field.default_factory is dataclasses.MISSING
                    else field.default_factory
                )
            non_init_default_values: list[Any] = []
            non_init_default_factories: list[Any] = []
            for name in non_init_names:
                decode_children.append(marker.construction_schemas[name]._node)
                field = fields_by_name[name]
                non_init_default_values.append(
                    MISSING
                    if field.default is dataclasses.MISSING
                    else field.default
                )
                non_init_default_factories.append(
                    None
                    if field.default_factory is dataclasses.MISSING
                    else field.default_factory
                )
            decode_node = NativeSchema(
                "dataclass_decode",
                builtins.tuple(decode_children),
                (
                    cls,
                    builtins.tuple(names),
                    builtins.tuple(field_flags),
                    builtins.tuple(field_default_values),
                    builtins.tuple(field_default_factories),
                    builtins.tuple(non_init_names),
                    getattr(cls, "__post_init__", None) is not None,
                    builtins.tuple(non_init_default_values),
                    builtins.tuple(non_init_default_factories),
                    builtins.tuple(input_names),
                ),
            )
        else:
            decode_transform = NativeSchema(
                "async_transform" if decode_async_needed else "transform",
                (decode_input_schema._node,),
                (decode_value_async if decode_async_needed else decode_value,),
            )
            decode_node = NativeSchema(
                "pipe",
                (decode_transform, output_schema._node),
                (),
            )
        if marker is not None:
            encode_children: list[NativeSchema] = []
            for name in stored_names:
                schema = schemas_by_name[name]
                encode_children.append(
                    schema._encode_node
                    if isinstance(schema, CodecSchema)
                    else input_schemas[name]._node
                )
            encode_node = NativeSchema(
                "dataclass_encode",
                builtins.tuple(encode_children),
                (cls, builtins.tuple(stored_names)),
            )
        else:
            encode_node = NativeSchema(
                "async_transform" if encode_async_needed else "transform",
                (output_schema._node,),
                (encode_value_async if encode_async_needed else encode_value,),
            )

        result = _CompoundCodecSchema[DataclassT, dict[str, Any]](
            decode_node,
            _encode_node=encode_node,
            _input_schema=input_schema,
            _output_schema=output_schema,
            _json_output_schema=output_json_schema,
            _inner=output_schema,
        )
        if recursive_codec is not None:
            recursion_holder["codec"] = result
            json_holder["input_schema"] = input_schema
            json_holder["output_json_schema"] = output_json_schema
        return result



_IDENTITY_ENCODE_KINDS = frozenset({
    "string", "number", "integer", "boolean", "unknown", "none",
    "exact_instance", "json", "native_enum", "literal", "enum", "instance",
    "length_min", "length_max", "min", "max", "exclusive_min", "exclusive_max",
    "multiple_of", "positive", "integral", "regex", "starts_with", "ends_with",
    "includes", "ipv6", "cidrv4", "cidrv6", "trim", "lower", "upper",
})


def _array_encode_node(
    schema: ArraySchema[Any, Any], item: NativeSchema,
) -> NativeSchema:
    constraints: list[builtins.tuple[str, builtins.tuple[Any, ...]]] = []
    node = schema._node
    while node.kind in _CONSTRAINT_KINDS:
        constraints.append((node.kind, node.args))
        node = node.children[0]
    result = NativeSchema("array", (item,), ())
    for kind, args in reversed(constraints):
        result = NativeSchema(kind, (result,), args)
    return result


def _set_encode_node(
    schema: SetSchema[Any, Any], item: NativeSchema,
) -> NativeSchema:
    constraints: list[builtins.tuple[str, builtins.tuple[Any, ...]]] = []
    node = schema._node
    while node.kind in _CONSTRAINT_KINDS:
        constraints.append((node.kind, node.args))
        node = node.children[0]
    result = NativeSchema("set_encode", (item,), ())
    for kind, args in reversed(constraints):
        result = NativeSchema(kind, (result,), args)
    return result


def _encode_node_for(schema: Schema[Any, Any]) -> NativeSchema | None:
    if isinstance(schema, CodecSchema):
        return schema._encode_node
    if isinstance(schema, ArraySchema):
        item_node = _encode_node_for(schema.item)
        if item_node is None:
            return None
        return _array_encode_node(schema, item_node)
    if isinstance(schema, SetSchema):
        item_node = _encode_node_for(schema.item)
        if item_node is None:
            return None
        return _set_encode_node(schema, item_node)
    if isinstance(schema, ObjectSchema):
        property_nodes: list[NativeSchema] = []
        for prop in schema.shape.values():
            prop_node = _encode_node_for(prop)
            if prop_node is None:
                return None
            property_nodes.append(prop_node)
        catchall_node = None
        if schema._catchall is not None:
            catchall_node = _encode_node_for(schema._catchall)
            if catchall_node is None:
                return None
        output_keys = builtins.tuple(
            key if (wire := _field_wire(prop._node)) is None else wire
            for key, prop in schema.shape.items()
        )
        return NativeSchema(
            "object",
            builtins.tuple(property_nodes),
            (
                output_keys,
                schema.unknown_keys,
                catchall_node,
                builtins.tuple(schema.shape),
            ),
        )
    if isinstance(schema, (KeyedRecordSchema, MapSchema)):
        key_node = _encode_node_for(schema.key_schema)
        value_node = _encode_node_for(schema.value_schema)
        if key_node is None or value_node is None:
            return None
        return NativeSchema("record", (value_node,), (key_node,))
    if isinstance(schema, RecordSchema):
        value_node = _encode_node_for(schema.value_schema)
        if value_node is None:
            return None
        return NativeSchema("record", (value_node,), (None,))
    if isinstance(schema, TupleSchema):
        tuple_nodes: list[NativeSchema] = []
        for item_schema in schema.items:
            item_node = _encode_node_for(item_schema)
            if item_node is None:
                return None
            tuple_nodes.append(item_node)
        rest_node = None
        if schema._rest is not None:
            rest_node = _encode_node_for(schema._rest)
            if rest_node is None:
                return None
        return NativeSchema("tuple", builtins.tuple(tuple_nodes), (rest_node,))
    if isinstance(schema, UnionSchema):
        option_nodes: list[NativeSchema] = []
        for option in schema.options:
            option_node = _encode_node_for(option)
            if option_node is None:
                return None
            option_nodes.append(option_node)
        return NativeSchema("union", builtins.tuple(option_nodes), ())
    if isinstance(schema, DiscriminatedUnionSchema):
        entries = list(schema.options.items())
        branch_nodes: list[NativeSchema] = []
        for _, option in entries:
            branch_node = _encode_node_for(option)
            if branch_node is None:
                return None
            branch_nodes.append(branch_node)
        return NativeSchema(
            "discriminated_union",
            builtins.tuple(branch_nodes),
            (schema.key, builtins.tuple(label for label, _ in entries)),
        )
    if isinstance(schema, IntersectionSchema):
        left_node = _encode_node_for(schema.left)
        right_node = _encode_node_for(schema.right)
        if left_node is None or right_node is None:
            return None
        return NativeSchema("intersection", (left_node, right_node), ())

    node = schema._node
    kind = node.kind
    if kind == "wire":
        if schema._inner is None:
            return None
        return _encode_node_for(schema._inner)
    if kind in ("optional", "nullable", "nonoptional", "required_output"):
        if schema._inner is None:
            return None
        wrapped_node = _encode_node_for(schema._inner)
        if wrapped_node is None:
            return None
        return NativeSchema(kind, (wrapped_node,), ())
    if kind in _IDENTITY_ENCODE_KINDS:
        return node
    return None
