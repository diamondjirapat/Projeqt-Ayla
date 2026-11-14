"""Application services required for user cache invalidation."""

from dataclasses import dataclass

from utils.i18n import I18nManager


@dataclass(frozen=True, slots=True)
class UserCacheDependencies:
    i18n: I18nManager
