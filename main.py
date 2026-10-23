"""Stable application entry point for Railway, Docker, systemd, and local runs."""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path


def _startup_hint(exc: BaseException) -> str:
    """Return a safe, actionable startup hint without exposing secrets."""
    message = str(exc).strip() or exc.__class__.__name__
    lowered = message.lower()
    if isinstance(exc, (ModuleNotFoundError, ImportError)) and ('telegram' in lowered or 'networkerror' in lowered):
        return ('Telegram dependency missing or incompatible. Use the same Python interpreter to run '
                'python -m pip install -r requirements.lock.txt, then python -m pip check. '
                'SENTINEL requires python-telegram-bot 22.8; the unrelated telegram package is not a substitute.')
    if "database_url_unresolved_reference" in lowered:
        return (
            "Railway did not resolve DATABASE_URL: verify the PostgreSQL service name in "
            "${{Postgres.DATABASE_URL}} and configure the variable on the Worker service."
        )
    if "database_url_invalid" in lowered or "database_url: " in lowered:
        return "DATABASE_URL is malformed. Use Railway's PostgreSQL service reference, without quotes or spaces."
    if "migration_backup_reference" in lowered or "migration backup" in lowered:
        return (
            "An existing database requires a VERIFIED migration backup before it can be modified. "
            "Do not disable this safety check: supply the required backup attestation and retry Pre-Deploy."
        )
    if "redis" in lowered and ("connect" in lowered or "name resolution" in lowered or "unresolved" in lowered):
        return "Check the Redis service name and REDIS_URL reference in Railway, and verify Redis is reachable."
    if "bot_token" in lowered or "invalid token" in lowered:
        return "BOT_TOKEN is missing or invalid. Add the exact token from @BotFather to Railway Variables (without quotes or spaces)."
    if "bot_username" in lowered or "telegram username" in lowered:
        return (
            "BOT_USERNAME is optional. Use only the username, such as MyBot_bot, or remove this Railway "
            "variable and the bot will detect its authenticated username automatically."
        )
    if "admin_ids" in lowered or "admin_id" in lowered:
        return "Set ADMIN_IDS to one or more numeric Telegram user IDs, for example: 123456789"
    if "channel_id" in lowered or "chat not found" in lowered:
        return (
            "Set PUBLIC_CHANNEL_ID and PRIVATE_CHANNEL_ID to numeric channel IDs starting with -100, "
            "and add the bot as an administrator in both channels."
        )
    if "persistent storage" in lowered or "persistent railway volume" in lowered:
        return (
            "Connect Railway PostgreSQL or mount a Railway Volume and set PERSISTENT_DATA_DIR to that mount path. "
            "Do not use ephemeral storage for production data."
        )
    if "permission denied" in lowered:
        return "The runtime cannot write to data/logs. Use the included Docker entrypoint or a writable Railway Volume."
    if "timezone" in lowered or "zoneinfo" in lowered:
        return "BOT_TIMEZONE must be an IANA timezone such as Asia/Tehran. The Docker image includes tzdata."
    if "another sentinel bot process" in lowered:
        return "Only one local bot process may use the same database. On Railway leave ENABLE_PROCESS_LOCK unset."
    if "conflict" in lowered and "getupdates" in lowered:
        return "Another deployment is still polling. Keep one Railway replica; this version retries until the old deployment stops."
    if "networkerror" in lowered or "connecterror" in lowered or "name resolution" in lowered:
        return "Telegram is temporarily unreachable. This version retries automatically; verify Railway outbound networking if it persists."
    return "Review the first STARTUP ERROR line in the logs and verify Railway Variables."


def _validate_packaged_runtime() -> None:
    """Verify that the extracted all-in-one release is complete."""
    project_root = Path(__file__).resolve().parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    try:
        from restore_runtime import ensure_runtime
    except ModuleNotFoundError as exc:
        if exc.name != "restore_runtime":
            raise RuntimeError(f"Unable to validate unified package: {exc}") from exc
        # The compact fallback archive intentionally excludes restore_runtime.py
        # to avoid embedding its own SHA-256 inside itself.  When that archive
        # is extracted as a standalone runtime, validate its essential tree
        # directly; the following application build/import remains the full
        # integrity check.
        essential = (
            "bot.py",
            "config.py",
            "requirements.txt",
            "VERSION",
            "data/database.json",
            "core/__init__.py",
            "core/db.py",
            "core/async_db.py",
            "core/async_primitives.py",
            "modules/__init__.py",
            "modules/admin_access.py",
            "modules/channel_management.py",
            "modules/management_suite.py",
            "modules/prices.py",
            "services/__init__.py",
            "ui/__init__.py",
        )
        missing = [name for name in essential if not (project_root / name).is_file()]
        if missing:
            raise RuntimeError("Unable to validate compact runtime; missing: " + ", ".join(missing)) from exc
        return
    try:
        ensure_runtime(project_root)
    except Exception as exc:
        # Raise a precise error before bot.py imports core/modules.
        raise RuntimeError(f"Unable to validate unified package: {exc}") from exc


def run_bot() -> None:
    """Import and start the bot lazily so startup failures are diagnosable."""
    _validate_packaged_runtime()
    from bot import main as bot_main

    bot_main()


def _startup_exit_code(exc: BaseException) -> int:
    """Do not endlessly retry invalid credentials, missing files or backup guards."""
    from core.postgres_runtime import error_code
    if isinstance(exc, (ImportError, SyntaxError, PermissionError)):
        return 78
    if error_code(exc) in {'DB_AUTH_FAILED', 'DB_DATABASE_MISSING', 'DB_PERMISSION_DENIED'}:
        return 78
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).lower()
        if current.__class__.__name__ == 'InvalidToken' or any(marker in message for marker in (
            'database_url_invalid', 'database_url_unresolved_reference', 'migration_backup_reference',
            'migration backup', 'backup checksum mismatch', 'unable to validate unified package',
            'unable to validate compact runtime', 'settings validation failed',
            'invalid configuration',
        )):
            return 78
        current = current.__cause__ or current.__context__
    return 1


def run_build_check() -> None:
    """Run an offline, side-effect-isolated application build check.

    This mode is safe for Docker/Railway build stages: it uses a temporary
    database and placeholder Telegram values, performs no network requests,
    builds every handler, and renders representative BUY/SELL signal images.
    """
    project_root = Path(__file__).resolve().parent
    previous: dict[str, str | None] = {}
    with tempfile.TemporaryDirectory(prefix="sentinel-build-check-") as temp_dir:
        temp = Path(temp_dir)
        placeholder_bot_token = f"{123456789}:{chr(65) * 32}"
        overrides = {
            "BOT_TOKEN": placeholder_bot_token,
            "ADMIN_IDS": "123456789",
            "PUBLIC_CHANNEL_ID": "-1001111111111",
            "PRIVATE_CHANNEL_ID": "-1002222222222",
            "DB_FILE": str(temp / "sentinel.sqlite3"),
            "DATABASE_URL": "",
            "POSTGRES_URL": "",
            "REDIS_URL": "",
            "GOOGLE_SHEETS_BACKUP_ENABLED": "false",
            "GOOGLE_SHEETS_PROJECTION_ENABLED": "false",
            "LOG_DIR": str(temp / "logs"),
            "BACKUP_DIR": str(temp / "backups"),
            "HEARTBEAT_FILE": str(temp / "heartbeat.json"),
            "PERSISTENCE_FILE": str(temp / "telegram-state.sqlite3"),
            "METRICS_FILE": str(temp / "metrics.json"),
            "WEBAPP_URL": "",
            "WEBAPP_SESSION_SECRET": "build-check-session-secret-0123456789abcdef",
            "BACKUP_ENCRYPTION_KEY": "build-check-backup-secret-0123456789abcdef",
            "SENSITIVE_DATA_KEY": "build-check-sensitive-secret-0123456789abcdef",
            "ENABLE_PROCESS_LOCK": "false",
            "ALLOW_EPHEMERAL_STORAGE": "true",
            # The offline check creates HTTP clients but never sends requests.
            # Inherited SOCKS proxies must not add undeclared optional runtime
            # dependencies to a network-free build validation.
            **{key: None for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")},
        }
        for key, value in overrides.items():
            previous[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            _validate_packaged_runtime()
            from bot import build_application
            from config import SETTINGS
            from core.db import load_db
            from modules.signal_image import create_signal_image

            SETTINGS.validate()
            database = load_db()
            if not isinstance(database, dict) or "users" not in database:
                raise RuntimeError("database bootstrap returned an invalid payload")
            application = build_application()
            handler_count = sum(len(group) for group in application.handlers.values())
            if handler_count < 1:
                raise RuntimeError("application built without Telegram handlers")

            generated: list[Path] = []
            try:
                generated.append(Path(create_signal_image("XAUUSD", "2335.50", ["2340", "2347.50", "2355.20"], "2328.10", "BUY", "1")))
                generated.append(Path(create_signal_image("XAUUSD", "2335.50", ["2330", "2325", "2320"], "2342.00", "SELL", "1")))
                if any(not path.is_file() or path.stat().st_size < 10_000 for path in generated):
                    raise RuntimeError("signal image renderer produced an invalid output")
            finally:
                for path in generated:
                    path.unlink(missing_ok=True)

            version = (project_root / "VERSION").read_text(encoding="utf-8").strip()
            print(f"BUILD CHECK OK: SENTINEL {version}; handlers={handler_count}", flush=True)
        finally:
            for key, old_value in previous.items():
                if old_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_value


def main() -> None:
    """Start the bot or run the deterministic offline build check."""
    check_mode = "--check" in sys.argv[1:] or "--build-check" in sys.argv[1:]
    try:
        if check_mode:
            run_build_check()
            return
        # Lazy import lets us report configuration/import failures as startup
        # errors instead of an unhelpful bare `main.py failed` message.
        run_bot()
    except KeyboardInterrupt:
        print("SENTINEL stopped by operator.", file=sys.stderr, flush=True)
    except SystemExit:
        raise
    except BaseException as exc:  # startup/build boundary: always log root cause
        from core.postgres_runtime import error_code, redact_error
        label = "BUILD CHECK ERROR" if check_mode else "STARTUP ERROR"
        print(f"{label} [{exc.__class__.__name__}]: {redact_error(exc)}", file=sys.stderr, flush=True)
        if os.getenv('DATABASE_URL') or os.getenv('POSTGRES_URL'):
            print(f"DATABASE DIAGNOSTIC: {error_code(exc)}", file=sys.stderr, flush=True)
        print(f"STARTUP HINT: {_startup_hint(exc)}", file=sys.stderr, flush=True)
        print(redact_error(''.join(traceback.format_exception(exc))), file=sys.stderr, flush=True)
        raise SystemExit(1 if check_mode else _startup_exit_code(exc)) from exc


if __name__ == "__main__":
    main()
