import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_DIR_NAME = 'logs'
DEFAULT_LOG_RETENTION_DAYS = 14
_DATED_LOG_NAME = re.compile(r'^(\d{2}-\d{2}-\d{4})-\d+\.log$')


class _SuppressUvicornAccess200(logging.Filter):
    """Drop uvicorn access-log records for HTTP 200 responses only.

    Uvicorn logs every request (e.g. dashboard polling) as
    `INFO: ... "GET /api/... HTTP/1.1" 200 OK`, which drowns out real logs.
    Non-200 responses (4xx/5xx) still pass through so errors stay visible.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            args = record.args
            if isinstance(args, tuple) and len(args) >= 5 and args[4] in (200, '200'):
                return False
            if ' 200 OK' in record.getMessage():
                return False
        except Exception:
            return True
        return True


def suppress_uvicorn_access_200() -> None:
    """Attach the 200-suppression filter to the uvicorn access logger."""
    access_logger = logging.getLogger('uvicorn.access')
    if not any(isinstance(f, _SuppressUvicornAccess200) for f in access_logger.filters):
        access_logger.addFilter(_SuppressUvicornAccess200())


async def silence_uvicorn_200_after_startup(server) -> None:
    """Install the 200-suppression filter once a uvicorn server has started.

    Uvicorn reconfigures its own logging during startup, which would wipe a
    filter installed earlier. Waiting on ``server.started`` guarantees the
    filter lands after that reconfiguration. Safe to call for every server;
    a failure here must never take down the server task.
    """
    try:
        await server.started.wait()
    except Exception:
        return
    try:
        suppress_uvicorn_access_200()
    except Exception:
        pass


def resolve_dated_log_path(logs_dir: Path | str | None = None, on_date: date | None = None) -> Path:
    """Resolve a per-run log file inside the ``logs/`` directory.

    The file is named ``DD-MM-YYYY-N.log`` where ``N`` is the run counter for
    that date (1 for the first run of the day, incrementing while the name is
    taken) so concurrent-day restarts never overwrite each other. The
    directory is created when missing.
    """
    if logs_dir is None:
        logs_dir = _default_logs_dir()
    logs_path = Path(logs_dir)
    logs_path.mkdir(parents=True, exist_ok=True)
    prefix = (on_date or date.today()).strftime('%d-%m-%Y')
    run = 1
    while (logs_path / f'{prefix}-{run}.log').exists():
        run += 1
    return logs_path / f'{prefix}-{run}.log'


def _default_logs_dir() -> Path:
    return Path(__file__).resolve().parents[1] / LOG_DIR_NAME


def log_retention_days() -> int:
    """Days of dated logs to keep, from ``LOG_RETENTION_DAYS``; 0 or less keeps everything."""
    try:
        return int(os.environ.get('LOG_RETENTION_DAYS', DEFAULT_LOG_RETENTION_DAYS))
    except ValueError:
        return DEFAULT_LOG_RETENTION_DAYS


def prune_dated_logs(
    logs_dir: Path | str | None = None, keep_days: int | None = None, today: date | None = None
) -> list[Path]:
    """Delete ``DD-MM-YYYY-N.log`` files dated more than ``keep_days`` before ``today``.

    Only files following the dated naming scheme are touched. Returns the removed paths.
    """
    keep_days = log_retention_days() if keep_days is None else keep_days
    logs_path = Path(logs_dir) if logs_dir is not None else _default_logs_dir()
    if keep_days <= 0 or not logs_path.is_dir():
        return []
    cutoff = (today or date.today()) - timedelta(days=keep_days)
    removed = []
    for entry in logs_path.iterdir():
        match = _DATED_LOG_NAME.match(entry.name)
        if not match or not entry.is_file():
            continue
        try:
            if datetime.strptime(match.group(1), '%d-%m-%Y').date() < cutoff:
                entry.unlink()
                removed.append(entry)
        except (ValueError, OSError):
            continue
    return removed


def setup_logging(logs_dir: Path | str | None = None, on_date: date | None = None) -> Path | None:
    """Setup logging configuration.

    Direct runs log to ``logs/DD-MM-YYYY-N.log``. Under scripts/run_services.py
    the runner captures stdout into its own dated log file, so skip the file
    handler there to avoid duplicates. Returns the resolved log path, or None
    when runner-managed.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_path: Path | None = None
    if not os.environ.get('AYLA_RUNNER_MANAGED'):
        # Direct runs still log to a dated file under logs/. Under
        # scripts/run_services.py the runner captures stdout into its own
        # dated log file, so skip it to avoid duplicates.
        log_path = resolve_dated_log_path(logs_dir, on_date)
        handlers.append(logging.FileHandler(log_path, encoding='utf-8'))
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers,
    )
    # Best effort: uvicorn re-applies its own logging config on server
    # startup (wiping this), so each web cog re-installs it after startup.
    suppress_uvicorn_access_200()
    # Runner-managed runs share the same logs/ folder, so prune in both modes.
    removed = prune_dated_logs(logs_dir, today=on_date)
    if removed:
        logger.info('Removed %d log file(s) older than %d days', len(removed), log_retention_days())
    return log_path


# def create_embed(title: str, description: str = None, color: discord.Color = discord.Color.blue()) -> discord.Embed:
#     """Create a standard embed"""
#     embed = discord.Embed(title=title, description=description, color=color)
#     return embed
#
# def format_user_mention(user: Union[discord.User, discord.Member]) -> str:
#     """Format user mention with fallback"""
#     return f"{user.mention} ({user.display_name})"
#
# async def safe_send(channel, content=None, **kwargs):
#     """Safely send a message with error handling"""
#     try:
#         return await channel.send(content, **kwargs)
#     except discord.Forbidden:
#         logger.warning(f"No permission to send message in {channel}")
#     except discord.HTTPException as e:
#         logger.error(f"HTTP error sending message: {e}")
#     except Exception as e:
#         logger.error(f"Unexpected error sending message: {e}")
#     return None
