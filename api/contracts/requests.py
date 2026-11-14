"""Transport validation. Feature validators still own Discord and domain policy."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, create_model

from api.contracts.player import ExactId, SafeNumericId
from database.user_settings import SettingsRequest as SettingsRequest

Id = ExactId | SafeNumericId
Text = Annotated[StrictStr, Field(max_length=4000)]
Name = Annotated[StrictStr, Field(min_length=1, max_length=100, pattern=r'\S', strict=True)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


def request_model(schema_name: str, required: dict | None = None, **optional):
    # Omitted fields remain omitted when passed to PATCH/domain handlers. None
    # is only accepted when explicitly included in a field's annotation.
    fields = {key: (annotation, ...) for key, annotation in (required or {}).items()}
    fields.update({key: (annotation, None) for key, annotation in optional.items()})
    return create_model(schema_name, __base__=RequestModel, **fields)


GuildRequest = request_model('GuildRequest', {'guild_id': Id})
MessageRequest = request_model('MessageRequest', {'message_id': Id})
ModuleRequest = request_model(
    'ModuleRequest',
    {'guild_id': Id, 'module_id': StrictStr, 'enabled': StrictBool},
    config=dict | None,
)
PermissionsRequest = request_model(
    'PermissionsRequest',
    {'guild_id': Id, 'role_id': Id, 'capabilities': list[StrictStr]},
)
GreetingsRequest = request_model('GreetingsRequest', {'guild_id': Id, 'greetings': dict})
TrapChannelRequest = request_model('TrapChannelRequest', {'guild_id': Id, 'trap_channel': dict})
ReactionRoleRequest = request_model(
    'ReactionRoleRequest',
    {'guild_id': Id, 'channel_id': Id},
    title=Text,
    description=Text,
)
GuildSettingsRequest = request_model(
    'GuildSettingsRequest',
    {'guild_id': Id},
    prefix=Annotated[StrictStr, Field(min_length=1, max_length=20)],
    default_volume=Annotated[StrictInt, Field(ge=0, le=200)],
    music_channel_id=Id | None | Literal[''],
    music_message_id=Id | None | Literal[''],
    autoplay=StrictBool,
    autoplay_preset=StrictStr,
    autoplay_config=dict,
    twenty_four_seven=StrictBool,
    leveling_enabled=StrictBool,
    level_channel_id=Id | None | Literal[''],
    level_alert_config=dict,
    social_alerts=list[dict],
    stream_alerts=dict,
    locale=Literal['en', 'th'],
    auto_role_id=Id | None | Literal['', 'none', 'null'],
    reaction_roles=dict,
    reaction_role_names=dict,
    reaction_role_embeds=dict,
    reaction_role_inline=dict,
)
GuildSettingsRequest = create_model(
    'GuildSettingsRequest',
    __base__=GuildSettingsRequest,
    legacy_247=(StrictBool, Field(default=None, alias='247')),
)
CommandRequest = request_model(
    'CommandRequest',
    {'guild_id': Id, 'name': Name},
    response=Text,
    description=Text,
    is_embed=StrictBool,
    embed_config=dict | None,
    enabled=StrictBool,
)
CommandPreviewRequest = request_model(
    'CommandPreviewRequest',
    {'guild_id': Id},
    response=Text,
    args_text=Text,
    is_embed=StrictBool,
    embed_config=dict | None,
)
CommandNameRequest = request_model('CommandNameRequest', {'guild_id': Id, 'name': Name}, enabled=StrictBool)
GiveawayRequest = request_model(
    'GiveawayRequest',
    {'guild_id': Id, 'channel_id': Id, 'prize': Annotated[StrictStr, Field(min_length=1, max_length=1000)]},
    winners_count=Annotated[StrictInt, Field(ge=1, le=50)],
    duration=StrictStr,
    title=Text,
    embed=dict,
)
RerollRequest = request_model('RerollRequest', {'message_id': Id}, winners_count=Annotated[StrictInt, Field(ge=1, le=50)])
ScheduleFields = {
    'channel_id': Id,
    'kind': Literal['announcement', 'event'],
    'content': Annotated[StrictStr, Field(min_length=1, max_length=2000)],
    'title': Annotated[StrictStr, Field(max_length=256)],
    'starts_at': StrictStr,
    'recurrence': Literal['once', 'daily', 'weekly', 'monthly'],
    'event_end_at': StrictStr | None,
    'location': Annotated[StrictStr, Field(max_length=128)],
    'embed': dict,
}
ScheduleRequest = request_model(
    'ScheduleRequest',
    {'guild_id': Id, **{key: ScheduleFields[key] for key in ('channel_id', 'kind', 'content', 'starts_at')}},
    **{key: value for key, value in ScheduleFields.items() if key not in ('channel_id', 'kind', 'content', 'starts_at')},
)
SchedulePatch = request_model('SchedulePatch', {'guild_id': Id}, **ScheduleFields)
PanelRequest = request_model('PanelRequest', {'guild_id': Id}, panel=dict)
PublishPanelRequest = request_model('PublishPanelRequest', {'guild_id': Id, 'channel_id': Id})
TicketRequest = request_model(
    'TicketRequest',
    {'guild_id': Id},
    action=Literal['update', 'close', 'reopen', 'delete', 'note'],
    note=Text,
    priority=Literal['low', 'normal', 'high', 'urgent'],
    category_name=Name,
    assigned_staff_id=Id,
    name=Name,
)
ModerationRequest = request_model(
    'ModerationRequest',
    {'guild_id': Id, 'target_user_id': Id, 'action': Literal['warn', 'note', 'timeout', 'kick', 'ban', 'unban']},
    evidence=list[StrictStr],
    points=Annotated[StrictInt, Field(ge=0, le=100)],
    duration_seconds=Annotated[StrictInt, Field(ge=1, le=2419200)] | None,
    notify=StrictBool,
    reason=Text,
    internal_note=Text,
)
ModerationPatch = request_model(
    'ModerationPatch',
    {'guild_id': Id},
    evidence=list[StrictStr],
    points=Annotated[StrictInt, Field(ge=0, le=100)],
    status=Literal['active', 'expired', 'revoked'],
    reason=Text,
    internal_note=Text,
)
CaseCleanupRequest = request_model(
    'CaseCleanupRequest',
    {'guild_id': Id, 'channel_id': Id, 'amount': Annotated[StrictInt, Field(ge=1, le=100)]},
)
AppealRequest = request_model(
    'AppealRequest',
    {'guild_id': Id, 'case_id': Annotated[StrictInt, Field(ge=1)], 'statement': Text},
)
ReviewAppealRequest = request_model(
    'ReviewAppealRequest',
    {'guild_id': Id, 'status': Literal['pending', 'reviewing', 'accepted', 'declined']},
    reason=Text,
)
MusicAppearanceTarget = Literal['voice', 'channel']
MusicEmbedRequest = request_model('MusicEmbedRequest', {'guild_id': Id}, config=dict | None, target=MusicAppearanceTarget)
MusicEmbedResetRequest = request_model('MusicEmbedResetRequest', {'guild_id': Id}, target=MusicAppearanceTarget)
MusicPreviewRequest = request_model(
    'MusicPreviewRequest',
    {'guild_id': Id},
    config=dict | None,
    state=Literal['idle', 'now_playing'] | None,
    target=MusicAppearanceTarget,
)
SyncAutoplayRequest = request_model('SyncAutoplayRequest', guild_id=Id | None, autoplay=StrictBool)

PlaylistNameRequest = request_model('PlaylistNameRequest', {'name': Name})
PlaylistQueueRequest = request_model('PlaylistQueueRequest', {'name': Name, 'tracks': Annotated[list[dict], Field(min_length=1, max_length=1000)]})
PlaylistTrackRequest = request_model('PlaylistTrackRequest', {'playlist_name': Name, 'track': dict})
PlaylistRemoveRequest = request_model('PlaylistRemoveRequest', {'playlist_name': Name, 'index': Annotated[StrictInt, Field(ge=0)]})
PlaylistCoverRequest = request_model('PlaylistCoverRequest', {'playlist_name': Name}, cover_url=Annotated[StrictStr, Field(max_length=2048)])
LastfmTokenRequest = request_model('LastfmTokenRequest', {'token': Annotated[StrictStr, Field(min_length=1, max_length=256)]})
ScrobbleRequest = request_model('ScrobbleRequest', enabled=StrictBool)
