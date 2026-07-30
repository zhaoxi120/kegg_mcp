"""Validate JSON-shaped MCP arguments without a JSON serialization round trip."""

from __future__ import annotations

import types
from datetime import date, datetime, time
from enum import Enum
from typing import Annotated, Any, Literal, TypeVar, Union, cast, get_args, get_origin

from pydantic import BaseModel

_M = TypeVar("_M", bound=BaseModel)


def validate_tool_input(model: type[_M], arguments: dict[str, Any]) -> _M:
    """Apply JSON container/scalar semantics, then run strict Pydantic validation."""
    prepared = _prepare_model(model, arguments)
    return model.model_validate(prepared, strict=True)


def _prepare_model(model: type[BaseModel], value: object) -> object:
    if model.__pydantic_root_model__:
        return _prepare_value(model.model_fields["root"].annotation, value)
    if not isinstance(value, dict):
        return value
    prepared: dict[str, object] = dict(cast(dict[str, object], value))
    for name, field in model.model_fields.items():
        if name in prepared:
            prepared[name] = _prepare_value(field.annotation, prepared[name])
    return prepared


def _prepare_value(annotation: object, value: object) -> object:
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        return _prepare_value(arguments[0], value)
    if origin in {Union, types.UnionType}:
        union_value: object = value
        if isinstance(value, dict):
            mapping = cast(dict[object, object], value)
            union_value = mapping
            for option in arguments:
                candidate = get_args(option)[0] if get_origin(option) is Annotated else option
                if not isinstance(candidate, type) or not issubclass(candidate, BaseModel):
                    continue
                discriminator = candidate.model_fields.get("kind")
                if discriminator is None or get_origin(discriminator.annotation) is not Literal:
                    continue
                if mapping.get("kind") in get_args(discriminator.annotation):
                    return _prepare_value(candidate, union_value)
        for option in arguments:
            prepared = _prepare_value(option, union_value)
            if prepared is not union_value:
                return prepared
        return union_value
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _prepare_model(annotation, value)
    if origin is tuple and isinstance(value, (list, tuple)):
        item_annotation = arguments[0] if arguments else object
        items = cast(list[object] | tuple[object, ...], value)
        return tuple(_prepare_value(item_annotation, item) for item in items)
    if origin is list and isinstance(value, list):
        item_annotation = arguments[0] if arguments else object
        return [_prepare_value(item_annotation, item) for item in cast(list[object], value)]
    if origin is dict and isinstance(value, dict):
        value_annotation = arguments[1] if len(arguments) > 1 else object
        mapping = cast(dict[object, object], value)
        return {key: _prepare_value(value_annotation, item) for key, item in mapping.items()}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        try:
            return annotation(value)
        except (TypeError, ValueError):
            return value
    if annotation is datetime and isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if annotation is date and isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    if annotation is time and isinstance(value, str):
        try:
            return time.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value


__all__ = ["validate_tool_input"]
