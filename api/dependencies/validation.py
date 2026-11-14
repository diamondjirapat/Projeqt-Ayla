"""Fail route assembly early when required services are unavailable."""

from collections.abc import Callable
from dataclasses import fields
from types import UnionType
from typing import get_args, get_origin, get_type_hints


def validate_dependencies(value, expected_type) -> None:
    if not isinstance(value, expected_type):
        raise TypeError(f'Expected {expected_type.__name__} dependencies')
    annotations = get_type_hints(expected_type)
    for field in fields(value):
        dependency = getattr(value, field.name)
        annotation = annotations[field.name]
        origin = get_origin(annotation)
        nullable = origin is UnionType and type(None) in get_args(annotation)
        if dependency is None and not nullable:
            raise ValueError(f'{expected_type.__name__}.{field.name} is required')
        if origin is Callable and not callable(dependency):
            raise TypeError(f'{expected_type.__name__}.{field.name} must be callable')
        if isinstance(annotation, type) and dependency is not None and not isinstance(dependency, annotation):
            raise TypeError(f'{expected_type.__name__}.{field.name} must be a {annotation.__name__}')
