"""Discord delivery with explicit mention permissions and conservative crash recovery."""

from datetime import UTC, datetime, timedelta
import hashlib
import re

import discord

from services.social_providers import SocialEvent

COLORS = {'twitch': 0x9146FF, 'youtube': 0xFF0000, 'x': 0x1D9BF0}
DEFAULT_MESSAGES = {
    'twitch': '🔴 {streamer} is live on Twitch: **{title}**! {url}',
    'youtube': '🎬 New update from {channel}: **{title}**! {url}',
    'x': 'New post by @{handle}: {text} {url}',
}


def message_payload(entry: dict, event: SocialEvent, marker: str) -> dict:
    handle = entry['handle'].strip().lstrip('@')
    values = {
        'streamer': event.creator or handle,
        'channel': event.creator or handle,
        'handle': handle,
        'title': event.title,
        'text': event.text,
        'game': event.game,
        'url': event.url,
    }
    template = entry.get('customMessage') or DEFAULT_MESSAGES[entry['platform']]
    # Replace in one pass: creator content cannot inject another template variable.
    content = re.sub(r'\{(streamer|channel|handle|title|text|game|url)\}', lambda match: values[match[1]], template)
    role = str(entry.get('mentionRole') or 'none')
    mentions = discord.AllowedMentions(everyone=False, users=False, roles=False, replied_user=False)
    prefix = ''
    if role in {'everyone', 'here'}:
        prefix = f'@{role} '
        mentions.everyone = True
    elif role.isdigit():
        prefix = f'<@&{role}> '
        mentions.roles = [discord.Object(id=int(role))]
    embed = discord.Embed(
        title=(event.title or event.creator or handle)[:256],
        description=(event.text if event.kind == 'post' else event.game)[:3500],
        url=event.url,
        color=COLORS[entry['platform']],
    )
    embed.set_author(name=(event.creator or handle)[:256])
    if event.image.startswith('https://'):
        embed.set_image(url=event.image)
    embed.set_footer(text=f'{entry["platform"]} · {marker}')
    return {'content': (prefix + content)[:2000], 'embed': embed, 'allowed_mentions': mentions}


def attempt_marker(state: dict, event: SocialEvent) -> str:
    return 'Ayla #' + hashlib.sha256(f'{state["_id"]}:{event.key}'.encode()).hexdigest()[:24]


async def reconcile(channel, attempt: dict) -> bool:
    started_at = datetime.fromisoformat(attempt['started_at'])
    try:
        async for message in channel.history(limit=100, after=started_at - timedelta(seconds=5)):
            if message.author.id == attempt['bot_id'] and any(
                str(embed.footer.text or '').endswith(attempt['marker']) for embed in message.embeds
            ):
                return True
    except (discord.HTTPException, OSError, TimeoutError):
        return False
    return False


async def deliver(state_model, state: dict, pending: dict, entry: dict, bot, channel) -> str:
    event = SocialEvent(**pending['event'])
    if pending.get('attempt'):
        if await reconcile(channel, pending['attempt']):
            state['pending'].remove(pending)
            await state_model.update(state, pending=state['pending'], last_sent_at=datetime.now(UTC))
            return ''
        return 'delivery_uncertain'
    marker = attempt_marker(state, event)
    pending['attempt'] = {'marker': marker, 'started_at': datetime.now(UTC).isoformat(), 'bot_id': bot.user.id}
    if not await state_model.update(state, pending=state['pending']):
        return 'lease_lost'
    try:
        await channel.send(**message_payload(entry, event, marker))
    except discord.HTTPException as error:
        if 400 <= error.status < 500:
            pending.pop('attempt', None)
            await state_model.update(state, pending=state['pending'])
            return 'discord_permissions' if error.status in {403, 404} else 'discord_rejected'
        return 'delivery_uncertain'
    except (OSError, TimeoutError):
        return 'delivery_uncertain'
    state['pending'].remove(pending)
    # If this write fails, the stored attempt is recovered from channel history; it is never blindly resent.
    await state_model.update(state, pending=state['pending'], last_sent_at=datetime.now(UTC))
    return ''
