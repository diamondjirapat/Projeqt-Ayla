"""Credential-free diagnostics returned to authorized guild settings viewers."""

from config import Config
from database.connection import db_manager
from database.social_alerts import SocialAlertState, subscription_key
from runtime.bots import all_bots
from services.social_providers import ProviderError, provider_capabilities, setup_problem


async def social_runtime(guild_id: int, entries: list[dict]) -> dict:
    statuses = {}
    available = db_manager.db is not None
    if available:
        try:
            statuses = await SocialAlertState().statuses(guild_id)
        except Exception:
            available = False
    running = any(bot.is_ready() and (cog := bot.get_cog('SocialAlerts')) and cog.monitor.is_running() for bot in all_bots)
    alerts = {}
    for entry in entries:
        try:
            status = statuses.get(subscription_key(guild_id, entry), {})
        except ProviderError:
            status = {}
        problem = setup_problem(entry, Config)
        code = 'paused' if not entry['enabled'] else problem or ('worker_offline' if not running else status.get('code') or ('watching' if status.get('checked_at') else 'waiting_first_check'))
        if not available and not problem and entry['enabled'] and running:
            code = 'status_unavailable'
        alerts[entry['id']] = {**status, 'code': code}
    return {
        'enabled': Config.SOCIAL_ALERTS_ENABLED,
        'running': running,
        'platforms': provider_capabilities(Config),
        'alerts': alerts,
    }
