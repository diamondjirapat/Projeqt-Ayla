"""Internal authentication, safe errors, request correlation, and readiness."""

import asyncio
import json
import logging
import re
import secrets
import time
from uuid import uuid4

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from utils.cache import cache_metrics

logger = logging.getLogger(__name__)


def configure_operations(app, key_provider, bot_provider, database_provider):
    @app.middleware('http')
    async def internal_boundary(request: Request, call_next):
        candidate = request.headers.get('X-Request-ID', '')
        request_id = candidate if re.fullmatch(r'[a-zA-Z0-9_-]{8,64}', candidate) else str(uuid4())
        request.state.request_id = request_id
        started = time.monotonic()
        expected_key = key_provider()
        supplied_key = request.headers.get('X-Internal-Key', '')
        if not expected_key or not secrets.compare_digest(supplied_key, expected_key):
            response = JSONResponse(status_code=403, content={'detail': 'Forbidden'})
        else:
            try:
                response = await call_next(request)
            except Exception as exc:
                # Payloads, exception text, headers and query strings may contain
                # secrets or private moderation content. Never log them here.
                logger.error('internal_request_failed request_id=%s exception=%s', request_id, type(exc).__name__)
                response = JSONResponse(status_code=500, content={'detail': 'Internal error'})
        response.headers['X-Request-ID'] = request_id
        route = request.scope.get('route')
        logger.info(
            json.dumps(
                {
                    'event': 'http_request',
                    'request_id': request_id,
                    'method': request.method,
                    'route': getattr(route, 'path', '<unmatched>'),
                    'status': response.status_code,
                    'duration_ms': round((time.monotonic() - started) * 1000),
                }
            )
        )
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        detail = exc.detail if exc.status_code < 500 else 'Internal error'
        return JSONResponse(status_code=exc.status_code, content={'detail': detail}, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={'detail': 'Validation error'})

    @app.get('/internal/health')
    async def health():
        return {'status': 'ok'}

    @app.get('/internal/cache-metrics')
    async def caches(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return {'caches': cache_metrics()}

    @app.get('/internal/ready')
    async def ready():
        bot = bot_provider()
        manager = database_provider()
        if bot is None or not bot.is_ready() or manager.db is None:
            raise HTTPException(status_code=503, detail='Service unavailable')
        try:
            await asyncio.wait_for(manager.db.command('ping'), timeout=2)
        except Exception as exc:
            raise HTTPException(status_code=503, detail='Service unavailable') from exc
        return {'status': 'ready'}
