"""Module metadata and the built-in Ayla module registry.

The registry is deliberately in-memory: it describes what the running bot
supports, while per-guild enablement is persisted by ``GuildModuleModel``.
"""

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any


LifecycleHook = Callable[[int, bool], Awaitable[None]]


@dataclass(frozen=True)
class ModuleDefinition:
    module_id: str
    version: str = '1.0.0'
    default_enabled: bool = True
    required_bot_permissions: tuple[str, ...] = ()
    required_user_permissions: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    required_intents: tuple[str, ...] = ()
    slash_commands: tuple[str, ...] = ()
    web_routes: tuple[str, ...] = ()
    default_config: dict[str, Any] = field(default_factory=dict)
    migration_version: int = 1
    health_status: str = 'registered'
    enable_hook: str = 'persist_switch'
    disable_hook: str = 'persist_switch'

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ModuleRegistry:
    def __init__(self) -> None:
        self._modules: dict[str, ModuleDefinition] = {}
        self._enable_hooks: dict[str, LifecycleHook] = {}
        self._disable_hooks: dict[str, LifecycleHook] = {}

    def register(self, definition: ModuleDefinition) -> ModuleDefinition:
        if definition.module_id in self._modules:
            raise ValueError(f'Module already registered: {definition.module_id}')
        self._modules[definition.module_id] = definition
        return definition

    def get(self, module_id: str) -> ModuleDefinition | None:
        return self._modules.get(module_id)

    def all(self) -> tuple[ModuleDefinition, ...]:
        return tuple(self._modules.values())

    def register_hooks(
        self,
        module_id: str,
        *,
        on_enable: LifecycleHook | None = None,
        on_disable: LifecycleHook | None = None,
    ) -> None:
        if module_id not in self._modules:
            raise ValueError(f'Unknown module: {module_id}')
        if on_enable:
            self._enable_hooks[module_id] = on_enable
        if on_disable:
            self._disable_hooks[module_id] = on_disable

    async def run_hook(self, module_id: str, guild_id: int, enabled: bool) -> None:
        hook = (self._enable_hooks if enabled else self._disable_hooks).get(module_id)
        if hook:
            await hook(int(guild_id), bool(enabled))

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {definition.module_id: definition.as_dict() for definition in self.all()}


module_registry = ModuleRegistry()


def _register_builtins() -> None:
    definitions = (
        ModuleDefinition(
            'moderation_casebook',
            required_bot_permissions=(
                'view_audit_log',
                'moderate_members',
                'kick_members',
                'ban_members',
                'manage_messages',
            ),
            required_user_permissions=('moderate_members',),
            required_capabilities=('manage_moderation',),
            slash_commands=(
                'warn',
                'note',
                'timeout',
                'kick',
                'ban',
                'unban',
                'case',
                'revoke-case',
                'warnings',
                'appeal',
                'review-appeal',
                'case-cleanup',
            ),
            web_routes=('/api/guild/moderation',),
            default_config={
                'warning_decay_days': 90,
                'case_retention_days': 3650,
                'appeals_enabled': True,
                'escalation_rules': [],
            },
        ),
        ModuleDefinition(
            'tickets',
            required_bot_permissions=(
                'manage_channels',
                'view_channel',
                'send_messages',
                'embed_links',
                'read_message_history',
            ),
            required_user_permissions=('manage_channels',),
            required_capabilities=('manage_tickets',),
            slash_commands=(
                'ticket panel',
                'ticket priority',
                'ticket transfer',
                'ticket rename',
                'ticket add-member',
                'ticket remove-member',
                'ticket note',
                'ticket delete',
            ),
            web_routes=('/api/guild/tickets',),
            default_config={'transcript_formats': ['html', 'markdown', 'json']},
        ),
        ModuleDefinition(
            'appeals',
            required_capabilities=('review_appeals',),
            slash_commands=('appeal', 'review-appeal'),
            web_routes=('/api/guild/moderation/appeals',),
        ),
        ModuleDefinition(
            'greetings',
            required_bot_permissions=('send_messages',),
            required_user_permissions=('manage_guild',),
            required_capabilities=('edit_configuration',),
            web_routes=('/api/guild/greetings',),
        ),
        ModuleDefinition(
            'trap_channel',
            required_bot_permissions=(
                'manage_channels',
                'manage_messages',
                'moderate_members',
                'send_messages',
                'embed_links',
            ),
            required_user_permissions=('manage_guild',),
            required_capabilities=('edit_configuration',),
            slash_commands=(
                'trapchannel',
                'trapchannel create',
                'trapchannel set',
                'trapchannel penalty',
                'trapchannel refresh',
                'trapchannel disable',
            ),
            web_routes=('/api/guild/trap-channel',),
        ),
        ModuleDefinition(
            'scheduler',
            required_bot_permissions=('send_messages', 'embed_links', 'manage_events'),
            required_user_permissions=('manage_guild',),
            required_capabilities=('edit_configuration',),
            slash_commands=('schedule create', 'schedule list', 'schedule cancel'),
            web_routes=('/api/guild/scheduler',),
        ),
    )
    for definition in definitions:
        module_registry.register(definition)


_register_builtins()
