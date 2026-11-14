"""Player wire models and the JSON Schema consumed by Bun.

Regenerate the schema with ``python -m api.contracts.player`` after changing
these models. Contract tests require the checked-in schema to match exactly.
"""

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, StrictBool, StrictStr, model_validator
from pydantic.json_schema import models_json_schema

ExactId = Annotated[StrictStr, Field(pattern=r'^[1-9][0-9]{0,18}$')]
SafeNumericId = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
Number = Annotated[float, Field(strict=True)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra='allow')

    @model_validator(mode='before')
    @classmethod
    def require_boolean_literal(cls, value):
        if isinstance(value, dict):
            for field in ('connected', 'success'):
                if field in cls.model_fields and field in value and type(value[field]) is not bool:
                    raise ValueError('Expected a JSON boolean')
        return value


class Track(WireModel):
    title: StrictStr
    author: StrictStr
    uri: StrictStr
    length: Number


class PlayerListItem(WireModel):
    guild_id: ExactId
    guild_name: StrictStr
    channel_name: StrictStr
    is_playing: StrictBool
    current_track: StrictStr | None = None


class PlayerList(RootModel[list[PlayerListItem]]):
    pass


class VoiceChannel(WireModel):
    guild_id: ExactId
    guild_name: StrictStr
    channel_id: ExactId
    channel_name: StrictStr


class ConnectedState(WireModel):
    model_config = ConfigDict(
        json_schema_extra={
            'anyOf': [
                {'required': ['queue', 'history']},
                {
                    'required': ['timeline_revision'],
                    'properties': {'timeline_revision': {'type': 'string', 'minLength': 1}},
                    'not': {'anyOf': [{'required': ['queue']}, {'required': ['history']}]},
                },
            ],
        }
    )

    @model_validator(mode='before')
    @classmethod
    def require_timeline_or_revision(cls, value):
        if isinstance(value, dict):
            has_queue, has_history = 'queue' in value, 'history' in value
            if has_queue != has_history:
                raise ValueError('Queue and history must be sent together')
            if not has_queue and not isinstance(value.get('timeline_revision'), str):
                raise ValueError('An omitted timeline requires a revision')
            if not has_queue and not value['timeline_revision']:
                raise ValueError('An omitted timeline requires a nonempty revision')
        return value

    connected: Literal[True]
    guild_id: ExactId
    guild_name: StrictStr
    channel_id: ExactId
    channel_name: StrictStr
    bitrate: Number
    is_playing: StrictBool
    is_paused: StrictBool
    volume: Number
    position: Number
    loop_mode: Literal['OFF', 'TRACK', 'QUEUE']
    autoplay: StrictBool
    gapless: StrictBool = True
    current_track: Track | None
    queue: list[Track] = Field(default_factory=list)
    history: list[Track] = Field(default_factory=list)
    timeline_revision: StrictStr | None = None


class DisconnectedState(WireModel):
    connected: Literal[False]
    msg: StrictStr | None = None
    guild_id: None = None
    channel_id: None = None
    user_voice: VoiceChannel | None = None
    volume: Number = 25
    current_track: None = None
    queue: list[Track] = Field(default_factory=list)
    history: list[Track] = Field(default_factory=list)


PlayerState = Annotated[ConnectedState | DisconnectedState, Field(discriminator='connected')]


class PlayerStateContract(RootModel[PlayerState]):
    pass


class PlayerControl(WireModel):
    action: Literal[
        'play',
        'pause',
        'resume',
        'skip',
        'previous',
        'volume',
        'seek',
        'shuffle',
        'repeat',
        'autoplay',
        'queue_remove',
        'queue_jump',
        'history_jump',
        'queue_clear',
        'queue_move',
        'history_move',
        'history_to_queue',
        'queue_to_history',
        'current_to_queue',
        'current_to_history',
    ]
    value: Any = None
    guild_id: ExactId | SafeNumericId | None = None


class ControlResult(WireModel):
    success: Literal[True]


class SearchResult(WireModel):
    mode: StrictStr
    is_playlist: StrictBool
    playlist_name: StrictStr | None
    playlist_url: StrictStr | None = None
    tracks: list[Track]
    playlists: list[dict]


def export_schema() -> dict:
    _, schema = models_json_schema(
        [
            (model, 'validation')
            for model in (
                PlayerList,
                PlayerStateContract,
                PlayerControl,
                ControlResult,
                SearchResult,
            )
        ],
        ref_template='#/$defs/{model}',
    )
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema', **schema}


if __name__ == '__main__':
    destination = Path(__file__).with_name('player.schema.json')
    destination.write_text(json.dumps(export_schema(), indent=2) + '\n', encoding='utf-8')
