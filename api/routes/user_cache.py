"""Invalidate process-local user preferences after acknowledged dashboard writes."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Header

from api.dependencies.user_cache import UserCacheDependencies
from api.dependencies.validation import validate_dependencies


def register_routes(app: FastAPI, provide: Callable[[], UserCacheDependencies]) -> dict[str, Callable[..., Any]]:
    validate_dependencies(provide(), UserCacheDependencies)

    @app.post('/internal/user/invalidate-locale')
    async def internal_invalidate_user_locale(user_id: int = Header(..., alias='X-User-Id')):
        # The user is supplied by the authenticated gateway, never by the request body.
        services = provide()
        services.i18n.clear_user_cache(user_id)
        return {'success': True}

    return {'internal_invalidate_user_locale': internal_invalidate_user_locale}
