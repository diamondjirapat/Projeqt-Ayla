"""Conservative recovery of Discord sends without automatic duplicate delivery."""

import hashlib
from datetime import UTC, timedelta


class DeliveryUncertain(RuntimeError):
    """Leave the durable attempt for lease recovery and history reconciliation."""


def delivery_marker(item) -> str:
    occurrence = f'{item["_id"]}:{item["next_run_at"].isoformat()}'
    return 'Ayla #' + hashlib.sha256(occurrence.encode()).hexdigest()[:24]


async def reconcile_delivery(model, item, channel, now) -> None:
    """Complete a confirmed send, or fail closed when history cannot confirm it.

    Absence in history is not proof of non-delivery (deletion, delayed acceptance,
    or history limits). Never send again automatically for this attempt.
    """
    attempt = item['delivery_attempt']
    started_at = attempt['started_at']
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    try:
        async for message in channel.history(limit=1000, after=started_at - timedelta(seconds=5)):
            if message.author.id != attempt['bot_id']:
                continue
            if any(str(embed.footer.text or '').endswith(attempt['marker']) for embed in message.embeds):
                await model.resolve_item(item['_id'], now, claim_token=item['claim_token'])
                return
        await model.mark_failed(
            item['_id'],
            'Delivery could not be confirmed. Review channel history before manually scheduling a replacement.',
            claim_token=item['claim_token'],
        )
    except Exception as error:
        raise DeliveryUncertain('History reconciliation is unavailable; preserving the attempt') from error
