"""Canonical user preference policy shared with Bun through exported JSON Schema."""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, create_model


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


PREFERENCE_FIELDS = {
    'theme': Literal['dark', 'light', 'fantasy'],
    'accent': Literal['blue', 'purple', 'green', 'orange', 'dynamic'],
    'transparency': Annotated[StrictInt, Field(ge=0, le=20)],
    'blur': Annotated[StrictInt, Field(ge=0, le=10)],
    'corners': Annotated[StrictInt, Field(ge=0, le=32)],
    'animations': StrictBool,
    'startup': Literal['home', 'discover', 'library', 'history', 'playlists'],
    'autoplay': StrictBool,
    'language': Literal['en', 'th'],
    'locale': Literal['en', 'th'],
}
SettingsRequest = create_model(
    'SettingsRequest',
    __base__=SettingsModel,
    **{name: (annotation, None) for name, annotation in PREFERENCE_FIELDS.items()},
)
