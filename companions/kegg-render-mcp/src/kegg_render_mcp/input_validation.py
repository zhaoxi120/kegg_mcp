"""Validate JSON-shaped tool arguments without serializing them again."""

from __future__ import annotations

import types
from enum import Enum
from typing import Annotated, Any, TypeVar, Union, cast, get_args, get_origin

from pydantic import BaseModel

_M = TypeVar("_M", bound=BaseModel)


def validate_tool_input(model: type[_M], arguments: dict[str, Any]) -> _M:
    return model.model_validate(_prepare_model(model, arguments), strict=True)


def _prepare_model(model: type[BaseModel], value: object) -> object:
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
        for option in arguments:
            prepared = _prepare_value(option, value)
            if prepared is not value:
                return prepared
        return value
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _prepare_model(annotation, value)
    if origin is tuple and isinstance(value, (list, tuple)):
        item_annotation = arguments[0] if arguments else object
        items = cast(list[object] | tuple[object, ...], value)
        return tuple(_prepare_value(item_annotation, item) for item in items)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        try:
            return annotation(value)
        except (TypeError, ValueError):
            return value
    return value


__all__ = ["validate_tool_input"]
