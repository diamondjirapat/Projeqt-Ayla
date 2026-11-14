"""Versioned feature contracts exported to the Bun gateway from Python."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, RootModel, StrictBool, StrictInt, StrictStr, create_model
from pydantic.json_schema import models_json_schema

from api.contracts import requests as req
from api.contracts.player import ControlResult, PlayerControl, PlayerList, PlayerStateContract, SearchResult


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra='allow', strict=True)


def response_model(schema_name: str, **fields):
    return create_model(schema_name, __base__=ResponseModel, **{key: (value, ...) for key, value in fields.items()})


Success = response_model('Success', success=StrictBool)
Admin = response_model('Admin', is_admin=StrictBool)
GuildSummary = response_model('GuildSummary', id=req.Id, name=StrictStr, icon=StrictStr)
GuildList = type('GuildList', (RootModel[list[GuildSummary]],), {})
Modules = response_model('Modules', guild_id=req.Id, modules=list[dict])
Permissions = response_model('Permissions', guild_id=req.Id, capabilities=list[StrictStr], roles=list[dict], grants=dict, can_edit=StrictBool)
GuildSettings = response_model('GuildSettings', guild_id=req.Id, guild_name=StrictStr, can_edit=StrictBool, prefix=StrictStr)
YouTubeChannel = response_model('YouTubeChannel', id=StrictStr, name=StrictStr, url=StrictStr, avatar=StrictStr)
Greetings = response_model('Greetings', guild_id=req.Id, greetings=dict, can_edit=StrictBool)
GreetingsSaved = response_model('GreetingsSaved', success=StrictBool, greetings=dict)
TrapChannel = response_model('TrapChannel', guild_id=req.Id, trap_channel=dict, can_edit=StrictBool)
TrapChannelSaved = response_model('TrapChannelSaved', success=StrictBool, trap_channel=dict)
ReactionRoleMessage = response_model('ReactionRoleMessage', message_id=req.Id)
Commands = response_model('Commands', commands=list[dict])
CommandPreview = response_model('CommandPreview', parsed_text=StrictStr, parsed_embed=dict | None, preview_context=dict)
CommandSaved = response_model('CommandSaved', success=StrictBool, command=dict)
Giveaways = response_model('Giveaways', giveaways=list[dict], can_manage=StrictBool)
GiveawaySaved = response_model('GiveawaySaved', success=StrictBool, giveaway=dict)
GiveawayEntry = response_model('GiveawayEntry', success=StrictBool, entered=StrictBool, entries_count=StrictInt)
GiveawayReroll = response_model('GiveawayReroll', success=StrictBool, winners=list[req.Id])
ScheduleList = response_model('ScheduleList', items=list[dict], can_manage=StrictBool)
ScheduleSaved = response_model('ScheduleSaved', success=StrictBool, item=dict)
Tickets = response_model('Tickets', items=list[dict], total=StrictInt)
TicketDetail = response_model('TicketDetail', ticket=dict, events=list[dict])
TicketSaved = response_model('TicketSaved', success=StrictBool, ticket=dict)
PanelSaved = response_model('PanelSaved', success=StrictBool, panel=dict | None)
PanelPublished = response_model('PanelPublished', success=StrictBool, channel_id=req.Id, message_id=req.Id)
Paginated = response_model('Paginated', items=list[dict], total=StrictInt, page=StrictInt, limit=StrictInt)
CaseSaved = response_model('CaseSaved', success=StrictBool, case=dict | None)
Members = response_model('Members', items=list[dict])
EvidenceSaved = response_model('EvidenceSaved', success=StrictBool, url=StrictStr, size=StrictInt, expires_at=StrictStr)
Warnings = response_model('Warnings', points=StrictInt, warning_count=StrictInt, items=list[dict])
AppealSaved = response_model('AppealSaved', success=StrictBool, appeal=dict)
Leaderboard = response_model('Leaderboard', entries=list[dict], total=StrictInt, page=StrictInt, limit=StrictInt)
MusicEmbed = response_model('MusicEmbed', success=StrictBool, config=dict, banner_url=StrictStr, bar_url=StrictStr)
MusicPreview = response_model('MusicPreview', success=StrictBool, previews=dict, banner_url=StrictStr, bar_url=StrictStr)
Lyrics = response_model('Lyrics', found=StrictBool, lyrics=dict | None)
AutoplaySynced = response_model('AutoplaySynced', synced=StrictBool)
Health = response_model('Health', status=Literal['ok', 'ready'])
CacheMetrics = response_model('CacheMetrics', caches=list[dict])


@dataclass(frozen=True)
class Contract:
    method: str
    path: str
    response: type[BaseModel] | None
    request: type[BaseModel] | None = None


CONTRACTS = [
    Contract('POST', '/internal/user/invalidate-locale', Success),
    Contract('GET', '/internal/health', Health),
    Contract('GET', '/internal/cache-metrics', CacheMetrics),
    Contract('GET', '/internal/ready', Health),
    Contract('GET', '/internal/auth/check-admin', Admin),
    Contract('GET', '/internal/guilds/manageable', GuildList),
    Contract('GET', '/internal/guild/modules', Modules),
    Contract('POST', '/internal/guild/modules', Success, req.ModuleRequest),
    Contract('GET', '/internal/guild/permissions', Permissions),
    Contract('POST', '/internal/guild/permissions', Success, req.PermissionsRequest),
    Contract('GET', '/internal/guild/settings', GuildSettings),
    Contract('GET', '/internal/guild/autoplay/channel', YouTubeChannel),
    Contract('POST', '/internal/guild/settings', Success, req.GuildSettingsRequest),
    Contract('GET', '/internal/guild/greetings', Greetings),
    Contract('POST', '/internal/guild/greetings', GreetingsSaved, req.GreetingsRequest),
    Contract('GET', '/internal/guild/trap-channel', TrapChannel),
    Contract('POST', '/internal/guild/trap-channel', TrapChannelSaved, req.TrapChannelRequest),
    Contract('POST', '/internal/guild/reaction-roles/generate', ReactionRoleMessage, req.ReactionRoleRequest),
    Contract('GET', '/internal/guild/custom-commands', Commands),
    Contract('POST', '/internal/guild/custom-commands', CommandSaved, req.CommandRequest),
    Contract('POST', '/internal/guild/custom-commands/update', CommandSaved, req.CommandRequest),
    Contract('POST', '/internal/guild/custom-commands/delete', Success, req.CommandNameRequest),
    Contract('POST', '/internal/guild/custom-commands/toggle', Success, req.CommandNameRequest),
    Contract('POST', '/internal/guild/custom-commands/preview', CommandPreview, req.CommandPreviewRequest),
    Contract('GET', '/internal/guild/giveaways', Giveaways),
    Contract('POST', '/internal/guild/giveaways', GiveawaySaved, req.GiveawayRequest),
    Contract('POST', '/internal/guild/giveaways/enter', GiveawayEntry, req.MessageRequest),
    Contract('POST', '/internal/guild/giveaways/end', Success, req.MessageRequest),
    Contract('POST', '/internal/guild/giveaways/reroll', GiveawayReroll, req.RerollRequest),
    Contract('POST', '/internal/guild/giveaways/delete', Success, req.MessageRequest),
    Contract('GET', '/internal/guild/scheduler', ScheduleList),
    Contract('POST', '/internal/guild/scheduler', ScheduleSaved, req.ScheduleRequest),
    Contract('PATCH', '/internal/guild/scheduler/{item_id}', ScheduleSaved, req.SchedulePatch),
    Contract('DELETE', '/internal/guild/scheduler/{item_id}', Success),
    Contract('GET', '/internal/guild/tickets', Tickets),
    Contract('GET', '/internal/guild/tickets/{ticket_id}', TicketDetail),
    Contract('PATCH', '/internal/guild/tickets/{ticket_id}', TicketSaved, req.TicketRequest),
    Contract('GET', '/internal/guild/tickets/{ticket_id}/transcript', None),
    Contract('POST', '/internal/guild/tickets/panels', PanelSaved, req.PanelRequest),
    Contract('PATCH', '/internal/guild/tickets/panels/{panel_id}', PanelSaved, req.PanelRequest),
    Contract('DELETE', '/internal/guild/tickets/panels/{panel_id}', Success),
    Contract('POST', '/internal/guild/tickets/panels/{panel_id}/publish', PanelPublished, req.PublishPanelRequest),
    Contract('GET', '/internal/guild/moderation/cases', Paginated),
    Contract('POST', '/internal/guild/moderation/cases', CaseSaved, req.ModerationRequest),
    Contract('PATCH', '/internal/guild/moderation/cases/{case_id}', CaseSaved, req.ModerationPatch),
    Contract('POST', '/internal/guild/moderation/cases/{case_id}/cleanup', Success, req.CaseCleanupRequest),
    Contract('GET', '/internal/guild/moderation/cases/export', None),
    Contract('GET', '/internal/guild/moderation/my-cases', Paginated),
    Contract('GET', '/internal/guild/moderation/members', Members),
    Contract('GET', '/internal/guild/moderation/warnings/{target_user_id}', Warnings),
    Contract('POST', '/internal/guild/moderation/evidence/upload', EvidenceSaved),
    Contract('GET', '/internal/guild/moderation/evidence/{evidence_id}', None),
    Contract('GET', '/internal/guild/moderation/appeals', Paginated),
    Contract('POST', '/internal/guild/moderation/appeals', AppealSaved, req.AppealRequest),
    Contract('PATCH', '/internal/guild/moderation/appeals/{case_id}', Success, req.ReviewAppealRequest),
    Contract('GET', '/internal/leveling/guilds', GuildList),
    Contract('GET', '/internal/leveling/leaderboard/global', Leaderboard),
    Contract('GET', '/internal/leveling/leaderboard/server/{guild_id}', Leaderboard),
    Contract('GET', '/internal/guild/music-embed', MusicEmbed),
    Contract('POST', '/internal/guild/music-embed', MusicEmbed, req.MusicEmbedRequest),
    Contract('POST', '/internal/guild/music-embed/reset', MusicEmbed, req.MusicEmbedResetRequest),
    Contract('POST', '/internal/guild/music-embed/preview', MusicPreview, req.MusicPreviewRequest),
    Contract('GET', '/internal/player/list', PlayerList),
    Contract('GET', '/internal/player/state', PlayerStateContract),
    Contract('POST', '/internal/player/control', ControlResult, PlayerControl),
    Contract('GET', '/internal/player/search', SearchResult),
    Contract('GET', '/internal/player/lyrics', Lyrics),
    Contract('POST', '/internal/player/sync-autoplay', AutoplaySynced, req.SyncAutoplayRequest),
]
PUBLIC_REQUESTS = [
    req.SettingsRequest,
    req.PlaylistNameRequest,
    req.PlaylistQueueRequest,
    req.PlaylistTrackRequest,
    req.PlaylistRemoveRequest,
    req.PlaylistCoverRequest,
    req.LastfmTokenRequest,
    req.ScrobbleRequest,
]


def export_schema():
    models = {model for contract in CONTRACTS for model in (contract.response, contract.request) if model}
    models.update(PUBLIC_REQUESTS)
    _, schema = models_json_schema(
        [(model, 'validation') for model in sorted(models, key=lambda item: item.__name__)],
        ref_template='#/$defs/{model}',
    )
    return {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        **schema,
        'routes': [
            {
                'method': c.method,
                'path': c.path,
                'request': c.request.__name__ if c.request else None,
                'response': c.response.__name__ if c.response else None,
            }
            for c in CONTRACTS
        ],
    }


def export_types(schema: dict) -> str:
    def wire_type(value):
        if '$ref' in value:
            return value['$ref'].rsplit('/', 1)[-1]
        for union in ('anyOf', 'oneOf'):
            if union in value:
                return '(' + ' | '.join(wire_type(item) for item in value[union]) + ')'
        if 'const' in value:
            return json.dumps(value['const'])
        if 'enum' in value:
            return '(' + ' | '.join(json.dumps(item) for item in value['enum']) + ')'
        kind = value.get('type')
        if kind == 'array':
            return f'Array<{wire_type(value.get("items", {}))}>'
        if kind == 'object' or 'properties' in value:
            properties = value.get('properties', {})
            required = value.get('required', [])
            fields = [f'{json.dumps(key)}{"" if key in required else "?"}: {wire_type(item)}' for key, item in properties.items()]
            additional = value.get('additionalProperties', True)
            if additional:
                fields.append(f'[key: string]: {wire_type(additional) if isinstance(additional, dict) else "unknown"}')
            return '{ ' + '; '.join(fields) + ' }'
        return {'string': 'string', 'integer': 'number', 'number': 'number', 'boolean': 'boolean', 'null': 'null'}.get(kind, 'unknown')

    lines = ['// Generated by python -m api.contracts.features. Do not edit by hand.']
    lines += [f'export type {name} = {wire_type(value)}' for name, value in schema['$defs'].items()]
    lines.append('type InternalEndpoint =')
    for contract in CONTRACTS:
        if not contract.response:
            continue
        path = re.sub(r'\{\w+\}', '${string}', contract.path)
        lines.append(f'  | {{ method: {json.dumps(contract.method)}; path: `{path}`; response: {contract.response.__name__} }}')
    lines += [
        'type Match<Endpoint, Path extends string, Method extends string> =',
        '  Endpoint extends { method: Method; path: infer Template extends string; response: infer Payload }',
        '    ? Path extends Template ? Payload : never : never',
        'export type InternalPayload<Path extends string, Method extends string> =',
        '  [Match<InternalEndpoint, Path, Method>] extends [never] ? unknown : Match<InternalEndpoint, Path, Method>',
    ]
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    schema = export_schema()
    Path(__file__).with_name('features.schema.json').write_text(json.dumps(schema, indent=2) + '\n', encoding='utf-8')
    frontend = Path(__file__).resolve().parents[3] / 'frontend'
    if frontend.exists():
        (frontend / 'server/contracts/features.generated.ts').write_text(export_types(schema), encoding='utf-8')
        (frontend / 'contracts/features.schema.json').write_text(json.dumps(schema, indent=2) + '\n', encoding='utf-8')
