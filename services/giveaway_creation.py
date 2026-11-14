"""Keep Discord posting and persistence recoverable across ambiguous failures."""

import asyncio
import hashlib
import json
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from pymongo.errors import DuplicateKeyError
import discord

from fastapi import HTTPException

from services.serialization import _serialize_giveaway

logger = logging.getLogger(__name__)
POST_TIMEOUT = 20
CLEANUP_TIMEOUT = 10
RECORD_TIMEOUT = 5
RECOVERY_GRACE = timedelta(seconds=60)


async def recover_creation(operation_id, operation, channel, model, creations):
    if operation['status'] == 'compensated':
        return None
    created_at = operation['created_at']
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age = datetime.now(UTC) - created_at
    # A retry must not race the original handler's insert or orphan cleanup.
    # Its bounded work finishes before this recovery window opens.
    if operation['status'] in ('pending', 'posted') and age < RECOVERY_GRACE:
        return None
    message_id = operation.get('message_id')
    if not message_id:
        # A recent request may still be posting. History recovery can only adopt
        # a message from the exact original bot with this durable reference.
        if age < RECOVERY_GRACE:
            return None
        reference = f'Ayla creation: {operation_id}'
        async for message in channel.history(limit=1000, after=created_at):
            if message.author.id != operation['bot_id']:
                continue
            if any(reference in str(getattr(embed.footer, 'text', '') or '') for embed in message.embeds):
                message_id = message.id
                break
    if not message_id:
        return None
    existing = await model.get_giveaway(message_id)
    if not existing:
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            await creations.record(operation_id, 'compensated', message_id=message_id)
            return None
        reference = f'Ayla creation: {operation_id}'
        if message.author.id != operation['bot_id'] or not any(reference in str(getattr(embed.footer, 'text', '') or '') for embed in message.embeds):
            return None
        try:
            existing = await model.create_giveaway(message_id=message_id, **operation['payload'])
        except DuplicateKeyError:
            existing = await model.get_giveaway(message_id)
            if not existing:
                raise
    result = _serialize_giveaway(existing)
    await creations.record(operation_id, 'completed', result=result, message_id=message_id)
    return result


async def create_giveaway_safely(channel, embed, view, model, creations, payload, raw_request, idempotency_key=None):
    key = idempotency_key if isinstance(idempotency_key, str) else secrets.token_hex(16)
    if not re.fullmatch(r'[a-zA-Z0-9_-]{8,128}', key):
        raise HTTPException(status_code=400, detail='Invalid Idempotency-Key.')
    operation_id = hashlib.sha256(f'{payload["host_id"]}:{payload["guild_id"]}:{key}'.encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(raw_request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    created, operation = await creations.reserve(operation_id, fingerprint, payload, channel.guild.me.id)
    if not created:
        if not operation or operation['fingerprint'] != fingerprint:
            raise HTTPException(status_code=409, detail='Idempotency-Key was used with another payload.')
        if operation['status'] == 'completed':
            return operation['result']
        result = await recover_creation(operation_id, operation, channel, model, creations)
        if result:
            return result
        raise HTTPException(status_code=409, detail=f'Creation requires review. Reference: {operation_id}')

    footer = str(getattr(embed.footer, 'text', '') or '')
    reference = f'Ayla creation: {operation_id}'
    embed.set_footer(text=f'{footer[:1900]}\n{reference}'.strip(), icon_url=getattr(embed.footer, 'icon_url', None))
    message = None
    persisted = False
    try:
        # Reservation is durable before Discord is contacted. No automatic retry
        # is allowed after this boundary, including a send timeout/cancellation.
        async with asyncio.timeout(POST_TIMEOUT):
            message = await channel.send(embed=embed, view=view)
            await creations.record(operation_id, 'posted', message_id=message.id)
            document = await model.create_giveaway(message_id=message.id, **payload)
            persisted = True
            result = _serialize_giveaway(document)
            await creations.record(operation_id, 'completed', result=result)
            return result
    except (Exception, asyncio.CancelledError) as exc:
        status = 'uncertain'
        if message is not None and not persisted:
            try:
                # A database timeout can mean the insert committed. Check before
                # deleting a message that might already belong to a live giveaway.
                async with asyncio.timeout(CLEANUP_TIMEOUT):
                    existing = await model.get_giveaway(message.id)
                    if existing:
                        result = _serialize_giveaway(existing)
                        await creations.record(operation_id, 'completed', result=result, message_id=message.id)
                        if isinstance(exc, asyncio.CancelledError):
                            raise exc
                        return result
                    await message.delete()
                    status = 'compensated'
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        try:
            async with asyncio.timeout(RECORD_TIMEOUT):
                await creations.record(operation_id, status, **({'message_id': message.id} if message else {}))
        except Exception:
            pass  # The original reservation and embed reference remain durable.
        logger.error('giveaway_creation_failed operation_id=%s status=%s exception=%s', operation_id, status, type(exc).__name__)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise HTTPException(status_code=503, detail='Giveaway creation could not be completed.') from exc
