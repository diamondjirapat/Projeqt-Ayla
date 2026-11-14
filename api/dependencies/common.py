"""Callable contracts shared by internal API dependencies."""

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

import discord
from utils.modules import ModuleDefinition

PermissionCheck = Callable[[int, int], Awaitable[bool]]
CapabilityCheck = Callable[[int, int, str], Awaitable[bool]]
GuildLookup = Callable[[int], discord.Guild | None]
MemberLookup = Callable[[int, int], discord.Member | None]


@runtime_checkable
class PreviewContext(Protocol):
    def __call__(
        self,
        guild: discord.Guild,
        user: dict,
        *,
        member: discord.Member | None = None,
        channel: discord.abc.GuildChannel | None = None,
    ) -> dict[str, str | int]: ...


@runtime_checkable
class ModerationTargetValidator(Protocol):
    def __call__(
        self,
        guild: discord.Guild,
        actor: discord.Member | None,
        target: discord.Member,
        *,
        actor_is_owner: bool = False,
    ) -> None: ...


@runtime_checkable
class GuildPermission(Protocol):
    def __call__(self, guild_id: int, user_id: int, permission: str = 'manage_guild') -> Awaitable[bool]: ...


@runtime_checkable
class ModuleHealth(Protocol):
    def __call__(self, definition: ModuleDefinition, guild: discord.Guild, enabled: bool) -> dict: ...


@runtime_checkable
class GiveawayEmbedNormalizer(Protocol):
    def __call__(self, raw_config: object, *, strict: bool = False) -> dict[str, str]: ...
