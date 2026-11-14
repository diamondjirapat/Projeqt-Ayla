"""Shared MongoDB index policy, loaded from the manifest consumed by Bun."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from pymongo.collation import Collation


class IndexRequirementError(RuntimeError):
    """A required storage invariant could not be established."""


@dataclass(frozen=True)
class IndexSpec:
    collection: str
    keys: tuple[tuple[str, int], ...]
    unique: bool
    expire_after_seconds: int | None = None

    @property
    def required(self) -> bool:
        return self.unique or self.expire_after_seconds is not None

    def matches(self, metadata: dict[str, Any]) -> bool:
        return (
            tuple(tuple(key) for key in metadata.get('key', [])) == self.keys
            and bool(metadata.get('unique', False)) == self.unique
            and not metadata.get('sparse', False)
            and 'partialFilterExpression' not in metadata
            and metadata.get('collation', {}).get('locale', 'simple') == 'simple'
            and metadata.get('expireAfterSeconds') == self.expire_after_seconds
        )

    def create_options(self) -> dict:
        options = {'unique': self.unique, 'collation': Collation(locale='simple')}
        if self.expire_after_seconds is not None:
            options['expireAfterSeconds'] = self.expire_after_seconds
        return options


INDEX_SPECS = tuple(
    IndexSpec(
        collection=entry['collection'],
        keys=tuple(tuple(key) for key in entry['keys']),
        unique=entry['unique'],
        expire_after_seconds=entry.get('expireAfterSeconds'),
    )
    for entry in json.loads(Path(__file__).with_name('indexes.json').read_text(encoding='utf-8'))
)


def is_legacy_level_index(metadata: dict) -> bool:
    return metadata.get('unique') is True and list(metadata.get('key', [])) == [('user_id', 1)]


async def ensure_required_index(collection, spec: IndexSpec) -> None:
    """Accept matching custom names; never downgrade a failed unique index."""
    label = f'{spec.collection} {spec.keys}'
    try:
        indexes = await collection.index_information()
        if any(spec.matches(metadata) for metadata in indexes.values()):
            return
        await collection.create_index(list(spec.keys), **spec.create_options())
        indexes = await collection.index_information()
        if not any(spec.matches(metadata) for metadata in indexes.values()):
            raise IndexRequirementError(f'Required index was not verified: {label}')
    except IndexRequirementError:
        raise
    except Exception as error:
        # Driver errors may contain record values or credentials. Expose only
        # the constraint and exception category in the startup diagnostic.
        raise IndexRequirementError(
            f'Required index unavailable: {label} ({type(error).__name__}); run python -m scripts.audit_database'
        ) from error
