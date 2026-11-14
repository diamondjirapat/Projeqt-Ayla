"""Background social notifications. All bot replicas share MongoDB claims and durable delivery attempts."""

import asyncio
from datetime import UTC, datetime
import logging

from discord.ext import commands, tasks

from config import Config
from database.guilds import GuildModel
from database.social_alerts import SocialAlertState, subscription_key
from runtime.bots import all_bots, eligible_bot
from services.social_alerts import _normalize_social_alerts, _social_alerts_from_legacy
from services.social_delivery import deliver
from services.social_providers import ProviderError, SocialProviders, setup_problem, timestamp

logger = logging.getLogger(__name__)


def guild_entries(document: dict) -> list[dict]:
    return _normalize_social_alerts(document['social_alerts']) if document.get('social_alerts') is not None else _social_alerts_from_legacy(document.get('stream_alerts'))


class SocialAlerts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.guild_model = GuildModel()
        self.state_model = SocialAlertState()
        self.providers = SocialProviders()
        if Config.SOCIAL_ALERTS_ENABLED:
            self.monitor.start()

    async def cog_unload(self):
        task = self.monitor.get_task()
        self.monitor.cancel()
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass

    @tasks.loop(seconds=30)
    async def monitor(self):
        # One polling loop per process; leases also protect overlapping deployments.
        ready = [bot for bot in all_bots if bot.is_ready()]
        if ready and ready[0] is not self.bot:
            return
        try:
            guild_ids = list({guild.id for bot in ready or [self.bot] for guild in bot.guilds})
            if not guild_ids:
                return
            cursor = self.guild_model.collection.find(
                {
                    'guild_id': {'$in': guild_ids},
                    '$or': [{'social_alerts.0': {'$exists': True}}, {'stream_alerts': {'$exists': True}}],
                },
                {'guild_id': 1, 'social_alerts': 1, 'stream_alerts': 1},
            )
            async for document in cursor:
                await self.process_guild(document)
        except Exception as error:
            logger.error('social_monitor_failed exception=%s', type(error).__name__)

    @monitor.before_loop
    async def before_monitor(self):
        await self.bot.wait_until_ready()

    async def process_guild(self, document: dict):
        active_keys = []
        guild_id = document['guild_id']
        for entry in guild_entries(document):
            if not entry['enabled']:
                continue
            try:
                active_keys.append(subscription_key(guild_id, entry))
                state = await self.state_model.claim(guild_id, entry, datetime.now(UTC))
                if state:
                    await asyncio.wait_for(self.process_entry(guild_id, entry, state), timeout=90)
            except ProviderError:
                # Invalid handles are reported directly by the settings API.
                continue
            except Exception as error:
                logger.warning('social_alert_failed guild=%s alert=%s exception=%s', guild_id, entry['id'], type(error).__name__)
        await self.state_model.deactivate(guild_id, active_keys)

    async def process_entry(self, guild_id: int, entry: dict, state: dict):
        now = datetime.now(UTC)
        interval = self.providers.interval(entry['platform'])
        problem = setup_problem(entry)
        if problem:
            await self.state_model.finish(state, now, interval, problem)
            return
        bot = eligible_bot(guild_id, int(entry['channelId']))
        channel = bot.get_guild(guild_id).get_channel(int(entry['channelId'])) if bot else None
        if channel is None:
            await self.state_model.finish(state, now, interval, 'discord_permissions')
            return
        try:
            result = await self.providers.poll(entry['platform'], entry['handle'], state.get('metadata'), live='live' in entry['events'])
        except ProviderError as error:
            await self.state_model.finish(state, now, max(interval, error.retry_after), error.code)
            return
        seen = state.get('seen', [])
        pending = state.get('pending', [])
        events = sorted(
            (event for event in result.events if event.kind in entry['events']),
            key=lambda event: timestamp(event.published_at),
        )
        backlog = False
        if not state['initialized']:
            # Observe current content once, then announce only new events. Empty feeds are valid baselines.
            seen = [event.key for event in events]
            if not await self.state_model.update(state, initialized=True, baseline_at=now, seen=seen, pending=[], metadata=result.metadata):
                return
        else:
            baseline = state['baseline_at']
            if baseline.tzinfo is None:
                baseline = baseline.replace(tzinfo=UTC)
            for event in events:
                if event.key in seen:
                    continue
                if len(pending) >= 100:
                    backlog = True
                    break
                seen.append(event.key)
                if timestamp(event.published_at) >= baseline:
                    pending.append({'event': event.to_dict()})
            if not await self.state_model.update(state, seen=seen[-200:], pending=pending, metadata=result.metadata):
                return
        code = 'delivery_backlog' if backlog else result.warning
        # Read acknowledged settings immediately before delivery so pause/remove/channel changes take effect.
        current = await self.guild_model.get_guild(guild_id)
        latest = next((item for item in guild_entries(current or {}) if item['id'] == entry['id']), None)
        if not latest or not latest['enabled'] or subscription_key(guild_id, latest) != state['_id']:
            await self.state_model.finish(state, now, interval, 'paused')
            return
        fresh = [item for item in state['pending'] if not item.get('attempt')][:5]
        uncertain = [item for item in state['pending'] if item.get('attempt')][:2]
        for item in fresh + uncertain:
            outcome = await deliver(self.state_model, state, item, latest, bot, channel)
            if outcome:
                code = outcome
                if outcome != 'delivery_uncertain':
                    break
        if not code and any(not item.get('attempt') for item in state['pending']):
            interval = 30
        await self.state_model.finish(state, datetime.now(UTC), interval, code)


async def setup(bot):
    await bot.add_cog(SocialAlerts(bot))
