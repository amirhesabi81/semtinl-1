from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.runtime_topology import replica_count, replica_count_is_explicit

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def _load_local_env(path: Path) -> None:
    """Load a simple .env file without overriding real environment variables."""
    if not path.is_file():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
    except OSError:
        # Deployment managers such as Docker/Systemd inject variables directly;
        # an unreadable optional local .env must not hide those values.
        return


_load_local_env(BASE_DIR / ".env")
# The custom-emoji setup helper writes IDs to this separate file. Loading it
# automatically makes the designed icons active on the next bot restart while
# preserving the precedence of real deployment environment variables and .env.
_load_local_env(BASE_DIR / ".env.custom_emoji")


def _clean_env_value(name: str, raw: str) -> str:
    """Normalize values pasted from Railway, .env files, or copied code blocks."""
    value = str(raw or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩−–—", "01234567890123456789---"))
    if value.startswith("```") and value.endswith("```"):
        value = value.strip("`").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    # Accept accidental pastes such as ``BOT_TOKEN=123:abc`` in the value field.
    prefix = f"{name}="
    if value.upper().startswith(prefix.upper()):
        value = value[len(prefix):].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    return value


def _first_env(*names: str, default: str = "") -> str:
    """Return the first non-empty normalized environment value."""
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        cleaned = _clean_env_value(name, value)
        if cleaned:
            return cleaned
    return default


def _int(name: str, default: int = 0, *aliases: str) -> int:
    value = _first_env(name, *aliases, default=str(default))
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _float(name: str, default: float = 0.0, *aliases: str) -> float:
    value = _first_env(name, *aliases, default=str(default))
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc


def _ints(name: str, *aliases: str) -> tuple[int, ...]:
    raw = _first_env(name, *aliases)
    if not raw:
        return ()
    # Accept common Railway/UI formats: `1,2`, `1 2`, `[1, 2]`, or `1;2`.
    cleaned = raw.strip().strip("[](){}")
    parts = [part for part in re.split(r"[,;\s]+", cleaned) if part]
    try:
        return tuple(dict.fromkeys(int(part.strip().strip("'\"")) for part in parts))
    except ValueError as exc:
        raise RuntimeError(f"{name} must contain numeric Telegram user IDs") from exc


def _strings(name: str, *aliases: str) -> tuple[str, ...]:
    raw = _first_env(name, *aliases)
    if not raw:
        return ()
    values = [part.strip().strip("'\"") for part in re.split(r"[,;\n]+", raw) if part.strip()]
    return tuple(dict.fromkeys(value for value in values if value))


def _url(name: str) -> str:
    return _first_env(name).rstrip("/")


_TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


_PLACEHOLDER_MARKERS = (
    "change_me", "changeme", "your_secret", "default_secret", "test_secret",
    "example", "replace_me", "your-domain", "your_domain", "example.com",
)

def _looks_placeholder(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return bool(normalized) and any(marker in normalized for marker in _PLACEHOLDER_MARKERS)

def _production_environment() -> bool:
    environment = _first_env("SENTINEL_ENV", "ENVIRONMENT", "RAILWAY_ENVIRONMENT").lower()
    return environment in {"production", "prod"} or bool(
        _first_env("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")
    )

def _placeholder_url(value: str) -> bool:
    if not value:
        return False
    try:
        host = (urlsplit(value).hostname or "").lower()
    except ValueError:
        return True
    return not host or _looks_placeholder(host) or host.endswith((".invalid", ".example", ".localhost"))


def _normalize_telegram_username(raw: str, *, default: str = "", variable_name: str = "BOT_USERNAME") -> str:
    """Accept common Telegram username paste formats without blocking startup.

    Railway users often paste ``@name``, ``https://t.me/name``, a deep link,
    or even a label such as ``BOT_USERNAME=@name``.  Normalize all safe forms.
    When the value is still invalid, ignore it and use a safe fallback; the
    authenticated bot username is discovered from Telegram during post-init.
    """
    value = str(raw or "").strip()
    if not value:
        return default if _TELEGRAM_USERNAME_RE.fullmatch(default) else ""

    # Extract a username from Telegram URLs, including links with query strings.
    match = re.search(
        r"(?i)(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/@?([A-Za-z0-9_]{5,32})(?:[/?#]|$)",
        value,
    )
    if match:
        value = match.group(1)
    else:
        value = value.lstrip("@").split("?", 1)[0].split("#", 1)[0].strip("/ ")

    if _TELEGRAM_USERNAME_RE.fullmatch(value):
        return value

    fallback = default if _TELEGRAM_USERNAME_RE.fullmatch(default) else ""
    logger.warning(
        "%s was ignored because it is not a valid Telegram username; "
        "the bot will detect its real username automatically after connecting",
        variable_name,
    )
    return fallback


def _truthy_env(name: str, default: bool = False) -> bool:
    raw = _first_env(name, default="true" if default else "false").lower()
    return raw in {"1", "true", "yes", "on"}


def persistent_storage_status(*, database_url: str, db_file: Path) -> dict[str, object]:
    """Describe whether the canonical data backend survives a Railway redeploy."""
    if database_url:
        return {"persistent": True, "backend": "postgresql", "reason": "DATABASE_URL"}
    mount_raw = _first_env("PERSISTENT_DATA_DIR", "RAILWAY_VOLUME_MOUNT_PATH", "VOLUME_MOUNT_PATH")
    # A directory merely existing at /data does not prove it is a mounted
    # persistent Railway Volume. Require an explicit mount environment value.
    mounts = [Path(mount_raw).resolve()] if mount_raw else []
    resolved = db_file.resolve()
    for mount in mounts:
        try:
            resolved.relative_to(mount)
        except ValueError:
            continue
        return {"persistent": True, "backend": "sqlite-volume", "reason": str(mount)}
    return {"persistent": False, "backend": "sqlite-ephemeral", "reason": str(resolved)}


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_ids: tuple[int, ...]
    primary_admin_id: int
    public_channel_id: int
    private_channel_id: int
    staging_public_channel_id: int
    staging_private_channel_id: int
    bot_username: str
    support_username: str
    signal_brand_handle: str
    signal_brand_name: str
    webapp_url: str
    db_file: Path
    database_url: str
    log_dir: Path
    backup_dir: Path
    heartbeat_file: Path
    persistence_file: Path
    metrics_file: Path
    timezone: str
    vip_check_interval_seconds: int
    contest_check_interval_seconds: int
    order_notification_interval_seconds: int
    heartbeat_interval_seconds: int
    heartbeat_max_age_seconds: int
    request_timeout_seconds: int
    referral_batch_size: int
    referral_batch_points: int
    default_contest_duration_minutes: int
    image_render_concurrency: int
    signal_tracking_interval_seconds: int
    broadcast_batch_size: int
    broadcast_job_interval_seconds: int
    privacy_retention_days: int
    privacy_delete_grace_days: int
    health_host: str
    health_port: int
    external_backup_url: str
    backup_encryption_key: str
    sensitive_data_key: str
    backup_key_version: str
    backup_previous_keys: tuple[str, ...]
    redis_url: str
    concurrent_updates: int
    usdt_quote_valid_minutes: int
    usdt_verification_enabled: bool
    usdt_trongrid_api_key: str
    usdt_tron_api_url: str
    usdt_ethereum_rpc_url: str
    usdt_bsc_rpc_url: str
    gemini_enabled: bool
    gemini_api_key: str
    gemini_model: str
    gemini_api_base_url: str
    gemini_timeout_seconds: int
    gemini_max_retries: int
    gemini_user_assistant_enabled: bool
    gemini_admin_copilot_enabled: bool
    gemini_crm_enabled: bool
    gemini_channel_control_enabled: bool
    gemini_user_behavior_enabled: bool
    gemini_ticket_assistant_enabled: bool
    gemini_signal_analysis_enabled: bool
    gemini_risk_supervision_enabled: bool
    gemini_system_monitoring_enabled: bool
    gemini_user_daily_request_limit: int
    gemini_admin_daily_request_limit: int
    gemini_daily_token_limit: int
    gemini_monthly_token_limit: int
    gemini_data_retention_days: int
    gemini_context_max_chars: int
    gemini_healthcheck_ttl_seconds: int
    gemini_user_review_cache_seconds: int
    gemini_daily_report_enabled: bool
    gemini_daily_report_hour: int
    gemini_pseudonym_key: str
    gemini_context_cache_seconds: int
    gemini_autonomous_primary_admin: bool
    gemini_primary_admin_full_bot_access: bool
    gemini_delegated_admin_approval_required: bool
    gemini_max_tool_calls_per_request: int
    gemini_safety_threshold: str
    gemini_confirm_sensitive_actions: bool
    gemini_voice_commands_enabled: bool
    gemini_voice_model: str
    gemini_voice_max_seconds: int
    gemini_voice_max_bytes: int
    gemini_image_enabled: bool
    gemini_image_model: str
    gemini_image_fallback_model: str
    gemini_generated_ads_dir: str
    webapp_api_enabled: bool
    webapp_session_secret: str
    session_key_version: str
    session_previous_keys: tuple[str, ...]
    run_mode: str
    webhook_url: str
    webhook_listen: str
    webhook_port: int
    webhook_path: str
    webhook_secret_token: str
    webhook_max_connections: int
    sentry_dsn: str
    sentry_environment: str
    sentry_traces_sample_rate: float
    release_version: str
    release_build_number: str
    release_commit_sha: str
    release_image_digest: str

    @property
    def has_webapp(self) -> bool:
        # The launch button and the HTTP Mini App must obey the same feature
        # switch.  Previously an HTTPS URL kept the button visible even when
        # WEBAPP_API_ENABLED=false, leaving users on a deliberately disabled API.
        return self.webapp_api_enabled and self.webapp_url.startswith("https://")

    def validate(self) -> None:
        errors: list[str] = []
        token_pattern = re.compile(r"^[0-9]{6,12}:[A-Za-z0-9_-]{20,}$")
        if not token_pattern.fullmatch(self.bot_token):
            errors.append("BOT_TOKEN is missing or has an invalid format")
        if not self.admin_ids or any(value <= 0 for value in self.admin_ids):
            errors.append("ADMIN_IDS must contain positive numeric Telegram user IDs")
        if self.primary_admin_id <= 0:
            errors.append("PRIMARY_ADMIN_ID must be a positive numeric Telegram user ID")
        elif self.primary_admin_id not in self.admin_ids:
            errors.append("PRIMARY_ADMIN_ID must also be included in ADMIN_IDS")

        for name, value in (
            ("PUBLIC_CHANNEL_ID", self.public_channel_id),
            ("PRIVATE_CHANNEL_ID", self.private_channel_id),
        ):
            # Channel-dependent features remain disabled when an ID is omitted;
            # a missing optional channel must not take the whole bot offline.
            if value and (not str(value).startswith("-100") or len(str(abs(value))) < 10):
                errors.append(f"{name} must be a Telegram channel ID starting with -100")
        if self.public_channel_id and self.public_channel_id == self.private_channel_id:
            errors.append("PUBLIC_CHANNEL_ID and PRIVATE_CHANNEL_ID must be different")

        if self.webapp_url and not self.webapp_url.startswith("https://"):
            errors.append("WEBAPP_URL must use HTTPS")
        if self.webapp_url and _placeholder_url(self.webapp_url):
            errors.append("WEBAPP_URL contains a placeholder or invalid host")
        if self.run_mode not in {"polling", "webhook"}:
            errors.append("RUN_MODE must be polling or webhook")
        replicas = replica_count()
        if self.run_mode == "polling" and replicas > 1:
            errors.append("RUN_MODE=polling supports exactly one replica; use webhook mode for multiple replicas")
        if self.run_mode == "webhook":
            if not self.webhook_url.startswith("https://"):
                errors.append("WEBHOOK_URL must use HTTPS in webhook mode")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", self.webhook_secret_token):
                errors.append("WEBHOOK_SECRET_TOKEN must contain 1-256 letters, digits, underscore or hyphen")
            if not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", self.webhook_path):
                errors.append("WEBHOOK_PATH contains invalid characters")
            if _placeholder_url(self.webhook_url):
                errors.append("WEBHOOK_URL contains a placeholder or invalid host")
            # Webhook delivery is served by the unified FastAPI process on HEALTH_PORT/PORT.
            # WEBHOOK_PORT remains a backwards-compatible setting but is not a second listener.
        # Username variables are optional conveniences. They are normalized at
        # load time and the authenticated bot username is discovered from
        # Telegram after connection, so a bad pasted value must not take the
        # whole Railway service offline.
        if self.bot_username and not _TELEGRAM_USERNAME_RE.fullmatch(self.bot_username):
            logger.warning("Ignoring invalid BOT_USERNAME; Telegram getMe will provide the real username")
        if self.support_username and not re.fullmatch(r"[A-Za-z0-9_]{3,32}", self.support_username):
            logger.warning("Ignoring invalid SUPPORT_USERNAME")
        if not re.fullmatch(r"@[A-Za-z0-9_]{3,32}", self.signal_brand_handle):
            errors.append("SIGNAL_BRAND_HANDLE has an invalid format")
        if not 2 <= len(self.signal_brand_name) <= 40 or any(ord(ch) < 32 for ch in self.signal_brand_name):
            errors.append("SIGNAL_BRAND_NAME must be 2-40 printable characters")
        if self.health_port and self.health_port < 1024:
            logger.warning("HEALTH_PORT is below 1024 and may require elevated privileges")
        if self.database_url:
            # Diagnose Railway service-reference typos *before* opening a pool;
            # error codes contain no URL, password or hostname.
            from core.postgres_runtime import normalize_database_url
            try:
                normalize_database_url(self.database_url)
            except ValueError as exc:
                errors.append(f"DATABASE_URL: {exc}")
        if self.external_backup_url and not self.external_backup_url.startswith("https://"):
            errors.append("EXTERNAL_BACKUP_URL must use HTTPS")
        production = _production_environment()
        if self.gemini_enabled:
            if not self.gemini_api_key:
                errors.append("GEMINI_API_KEY is required when GEMINI_ENABLED=true")
            if self.gemini_api_base_url and not self.gemini_api_base_url.startswith("https://"):
                errors.append("GEMINI_API_BASE_URL must use HTTPS")
            if not re.fullmatch(r"[A-Za-z0-9._-]{2,120}", self.gemini_model):
                errors.append("GEMINI_MODEL has an invalid format")
            if len(self.gemini_pseudonym_key or "") < 16:
                errors.append("GEMINI_PSEUDONYM_KEY or its secure fallback must be at least 16 characters")
            if self.gemini_safety_threshold not in {
                "OFF", "BLOCK_NONE", "BLOCK_ONLY_HIGH", "BLOCK_MEDIUM_AND_ABOVE", "BLOCK_LOW_AND_ABOVE",
            }:
                errors.append("GEMINI_SAFETY_THRESHOLD is invalid")
            if production and not self.gemini_confirm_sensitive_actions:
                errors.append("GEMINI_CONFIRM_SENSITIVE_ACTIONS cannot be disabled in production")
            if production and self.gemini_safety_threshold in {"OFF", "BLOCK_NONE"}:
                errors.append("GEMINI_SAFETY_THRESHOLD must block at least medium-risk content in production")
            if self.gemini_image_enabled and not re.fullmatch(r"[A-Za-z0-9._-]{2,120}", self.gemini_image_model):
                errors.append("GEMINI_IMAGE_MODEL has an invalid format")
        if self.webapp_session_secret:
            if len(self.webapp_session_secret) < 32:
                errors.append("WEBAPP_SESSION_SECRET must be at least 32 characters")
            if _looks_placeholder(self.webapp_session_secret):
                errors.append("WEBAPP_SESSION_SECRET must not use an example/default value")
            if self.webapp_session_secret == self.bot_token or (self.bot_token and self.bot_token in self.webapp_session_secret):
                errors.append("WEBAPP_SESSION_SECRET must be independent from BOT_TOKEN")
        elif production and self.webapp_api_enabled:
            errors.append("WEBAPP_SESSION_SECRET is required in production")

        if self.backup_encryption_key and len(self.backup_encryption_key) < (32 if production else 16):
            errors.append("BACKUP_ENCRYPTION_KEY must be at least 32 characters in production")
        if self.sensitive_data_key and len(self.sensitive_data_key) < (32 if production else 16):
            errors.append("SENSITIVE_DATA_KEY must be at least 32 characters in production")
        if self.backup_encryption_key and _looks_placeholder(self.backup_encryption_key):
            errors.append("BACKUP_ENCRYPTION_KEY must not use an example/default value")
        if self.sensitive_data_key and _looks_placeholder(self.sensitive_data_key):
            errors.append("SENSITIVE_DATA_KEY must not use an example/default value")
        if production and self.webapp_api_enabled:
            if not self.backup_encryption_key:
                errors.append("BACKUP_ENCRYPTION_KEY is required in production")
            if not self.sensitive_data_key:
                errors.append("SENSITIVE_DATA_KEY is required in production")
        configured_secrets = [
            value for value in (
                self.webapp_session_secret,
                self.sensitive_data_key,
                self.backup_encryption_key,
            ) if value
        ]
        if len(configured_secrets) != len(set(configured_secrets)):
            errors.append(
                "WEBAPP_SESSION_SECRET, SENSITIVE_DATA_KEY and BACKUP_ENCRYPTION_KEY must be different"
            )
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", self.session_key_version):
            errors.append("WEBAPP_SESSION_KEY_VERSION has an invalid format")
        for row in self.session_previous_keys:
            version, separator, key = row.partition(":")
            if not separator or not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", version) or len(key) < 32:
                errors.append("Every WEBAPP_SESSION_PREVIOUS_KEYS entry must use version:secret with a 32+ character secret")
                break
        s3_endpoint = _first_env("RECEIPT_S3_ENDPOINT")
        if production and s3_endpoint:
            parsed_s3 = urlsplit(s3_endpoint)
            local_hosts = {"localhost", "127.0.0.1", "::1", "minio"}
            if parsed_s3.scheme != "https" and (parsed_s3.hostname or "").lower() not in local_hosts:
                errors.append("RECEIPT_S3_ENDPOINT must use HTTPS in production")
        if production and _first_env("RECEIPT_S3_BUCKET"):
            encryption = _first_env("RECEIPT_S3_SERVER_SIDE_ENCRYPTION")
            if encryption not in {"AES256", "aws:kms"}:
                errors.append("RECEIPT_S3_SERVER_SIDE_ENCRYPTION must be AES256 or aws:kms in production")
        if any(len(key) < 16 for key in self.backup_previous_keys):
            errors.append("Every BACKUP_PREVIOUS_KEYS entry must be at least 16 characters")
        storage = persistent_storage_status(database_url=self.database_url, db_file=self.db_file)
        on_railway = bool(_first_env("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID"))
        # Railway publishes a per-replica ID but not the total replica count to
        # each container. Never silently assume one replica in production: doing
        # so can enable process-local fallbacks and writable failover while the
        # service has actually been scaled out.
        if on_railway and not replica_count_is_explicit():
            errors.append(
                "SENTINEL_REPLICA_COUNT must be explicitly configured on Railway; "
                "set it to the total replica count and update it whenever scaling changes"
            )
        if on_railway and self.webapp_api_enabled and not self.redis_url:
            require_redis = _truthy_env("REQUIRE_REDIS_FOR_PRODUCTION", True)
            redis_override = _truthy_env("ALLOW_LOCAL_RATE_LIMIT", False)
            if require_redis and not redis_override and replicas > 1:
                errors.append(
                    "REDIS_URL is required when the production Mini App uses multiple replicas so rate limits "
                    "and idempotency remain consistent; attach Redis or return SENTINEL_REPLICA_COUNT to 1"
                )
            elif require_redis and not redis_override:
                logger.warning(
                    "REDIS_URL is not configured. Single-replica mode will use bounded in-process rate limits "
                    "and database-backed idempotency; add Redis before scaling beyond one replica."
                )
        if on_railway and self.webapp_api_enabled:
            receipt_persistent = bool(
                _first_env(
                    "RECEIPT_S3_BUCKET",
                    "RECEIPT_STORAGE_DIR",
                    "PERSISTENT_DATA_DIR",
                    "RAILWAY_VOLUME_MOUNT_PATH",
                    "VOLUME_MOUNT_PATH",
                )
            )
            require_receipts = _truthy_env("REQUIRE_PERSISTENT_RECEIPTS", True)
            if require_receipts and not receipt_persistent:
                errors.append(
                    "Persistent payment receipt storage is required: configure RECEIPT_S3_BUCKET (R2/S3) "
                    "or attach a Railway Volume and set RECEIPT_STORAGE_DIR/PERSISTENT_DATA_DIR; "
                    "REQUIRE_PERSISTENT_RECEIPTS=false is only for non-payment staging"
                )

        if on_railway:
            require_persistent_backups = _truthy_env("REQUIRE_PERSISTENT_BACKUPS", True)
            mount_raw = _first_env("PERSISTENT_DATA_DIR", "RAILWAY_VOLUME_MOUNT_PATH", "VOLUME_MOUNT_PATH")
            backup_on_volume = False
            if mount_raw:
                try:
                    self.backup_dir.resolve().relative_to(Path(mount_raw).resolve())
                    backup_on_volume = True
                except ValueError:
                    backup_on_volume = False
            google_backup_configured = bool(
                _truthy_env("GOOGLE_SHEETS_BACKUP_ENABLED", True)
                and _first_env("GOOGLE_SHEETS_SPREADSHEET_ID")
                and _first_env("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON", "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON_B64")
            )
            # Credentials alone are not evidence that a Sheet is writable or
            # restorable. Google Sheets remains a verified secondary/failover
            # target (runtime attestation proves read/write access), but it does
            # not replace an independently durable operational backup target at
            # startup because that attestation has not run yet.
            persistent_backup = bool(self.external_backup_url or backup_on_volume)
            if require_persistent_backups and bool(storage["persistent"]) and not persistent_backup:
                google_hint = (
                    " Google Sheets credentials are configured, but credentials alone do not count as "
                    "a persistent backup until runtime read/write attestation succeeds."
                    if google_backup_configured
                    else ""
                )
                errors.append(
                    "Persistent database storage requires an independently durable backup destination on Railway: "
                    "place BACKUP_DIR under an attached Volume or configure EXTERNAL_BACKUP_URL; "
                    "REQUIRE_PERSISTENT_BACKUPS=false is staging-only." + google_hint
                )

        if on_railway and not bool(storage["persistent"]):
            strict_persistence = _truthy_env("REQUIRE_PERSISTENT_STORAGE", self.webapp_api_enabled)
            emergency_override = _truthy_env("ALLOW_EPHEMERAL_STORAGE", False)
            if strict_persistence and not emergency_override:
                errors.append(
                    "Railway persistent storage is required: attach PostgreSQL (DATABASE_URL) or a Volume and set "
                    "PERSISTENT_DATA_DIR to its mount path; ALLOW_EPHEMERAL_STORAGE=true is an emergency override"
                )
            else:
                # A deployable package must not crash merely because an
                # external Railway resource has not been attached yet. Keep
                # serving with SQLite, expose a critical warning, and switch
                # automatically to PostgreSQL/Volume after configuration.
                logger.critical(
                    "Railway is running with EPHEMERAL storage (%s). The bot will start, but data can be lost "
                    "after redeploy. Attach PostgreSQL or a Volume. Set REQUIRE_PERSISTENT_STORAGE=true to "
                    "restore fail-closed startup.",
                    storage["reason"],
                )
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            errors.append("BOT_TIMEZONE is not a valid IANA timezone")
        if errors:
            raise RuntimeError("Invalid configuration: " + "; ".join(errors))


def _persistent_default(relative_name: str) -> str:
    """Prefer a mounted Railway volume when one is available.

    Railway services have an ephemeral application filesystem. A configured
    PostgreSQL DATABASE_URL remains the preferred backend; otherwise this
    helper automatically places SQLite state on the attached volume so restarts
    and redeployments do not erase users, orders, points, or conversations.
    """
    mount = _first_env("PERSISTENT_DATA_DIR", "RAILWAY_VOLUME_MOUNT_PATH", "VOLUME_MOUNT_PATH")
    if not mount and os.getenv("RAILWAY_ENVIRONMENT") and Path("/data").is_dir():
        # Common Railway volume mount. This is used only when the directory
        # actually exists; PostgreSQL through DATABASE_URL still has priority.
        mount = "/data"
    if mount:
        return str(Path(mount) / relative_name)
    return str(Path("data") / relative_name)


def _health_port() -> int:
    # Railway's router and health probe use PORT, even if an old HEALTH_PORT
    # variable remains in the service configuration.
    railway = bool(_first_env("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT"))
    port = _int("PORT", 8080, "HEALTH_PORT") if railway else _int("HEALTH_PORT", 0, "PORT")
    if not 0 <= port <= 65535 or (railway and port == 0):
        raise RuntimeError("HTTP health port must be between 1 and 65535 (0 disables HTTP only outside Railway)")
    return port


def load_settings() -> Settings:
    db_raw = os.getenv("DB_FILE", _persistent_default("sentinel.sqlite3")).strip()
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    log_raw = os.getenv("LOG_DIR", "logs").strip()
    log_dir = Path(log_raw)
    if not log_dir.is_absolute():
        log_dir = BASE_DIR / log_dir

    backup_raw = os.getenv("BACKUP_DIR", _persistent_default("backups")).strip()
    backup_dir = Path(backup_raw)
    if not backup_dir.is_absolute():
        backup_dir = BASE_DIR / backup_dir

    heartbeat_raw = os.getenv("HEARTBEAT_FILE", _persistent_default("heartbeat.json")).strip()
    heartbeat_file = Path(heartbeat_raw)
    if not heartbeat_file.is_absolute():
        heartbeat_file = BASE_DIR / heartbeat_file

    persistence_raw = os.getenv("PERSISTENCE_FILE", _persistent_default("telegram-state.sqlite3")).strip()
    persistence_file = Path(persistence_raw)
    if not persistence_file.is_absolute():
        persistence_file = BASE_DIR / persistence_file

    metrics_raw = os.getenv("METRICS_FILE", _persistent_default("metrics.json")).strip()
    metrics_file = Path(metrics_raw)
    if not metrics_file.is_absolute():
        metrics_file = BASE_DIR / metrics_file

    admin_ids = _ints("ADMIN_IDS", "TELEGRAM_ADMIN_IDS")
    legacy_admin = _int("ADMIN_ID", 0, "TELEGRAM_ADMIN_ID")
    if legacy_admin and legacy_admin not in admin_ids:
        admin_ids = (*admin_ids, legacy_admin)
    primary_admin_id = _int("PRIMARY_ADMIN_ID", admin_ids[0] if admin_ids else 0, "BOT_OWNER_ID", "OWNER_ID")
    if primary_admin_id and primary_admin_id not in admin_ids:
        admin_ids = (primary_admin_id, *admin_ids)

    return Settings(
        bot_token=_first_env("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN"),
        admin_ids=admin_ids,
        primary_admin_id=primary_admin_id,
        public_channel_id=_int("PUBLIC_CHANNEL_ID", 0, "PUBLIC_CHANNEL", "CHANNEL_ID"),
        private_channel_id=_int("PRIVATE_CHANNEL_ID", 0, "VIP_CHANNEL_ID", "PRIVATE_CHANNEL"),
        staging_public_channel_id=_int("STAGING_PUBLIC_CHANNEL_ID", 0),
        staging_private_channel_id=_int("STAGING_PRIVATE_CHANNEL_ID", 0),
        bot_username=_normalize_telegram_username(
            _first_env("BOT_USERNAME"),
            default="SENTINEL_bot",
            variable_name="BOT_USERNAME",
        ),
        support_username=_normalize_telegram_username(
            _first_env("SUPPORT_USERNAME"),
            default="",
            variable_name="SUPPORT_USERNAME",
        ),
        signal_brand_handle=(
            "@"
            + _normalize_telegram_username(
                _first_env("SIGNAL_BRAND_HANDLE"),
                default="SENTINEL",
                variable_name="SIGNAL_BRAND_HANDLE",
            )
        ),
        signal_brand_name=_first_env("SIGNAL_BRAND_NAME", default="SENTINEL"),
        webapp_url=_url("WEBAPP_URL"),
        db_file=db_path,
        database_url=_first_env("DATABASE_URL", "POSTGRES_URL"),
        log_dir=log_dir,
        backup_dir=backup_dir,
        heartbeat_file=heartbeat_file,
        persistence_file=persistence_file,
        metrics_file=metrics_file,
        timezone=_first_env("BOT_TIMEZONE", default="Asia/Tehran"),
        vip_check_interval_seconds=max(300, _int("VIP_CHECK_INTERVAL_SECONDS", 900)),
        contest_check_interval_seconds=max(60, _int("CONTEST_CHECK_INTERVAL_SECONDS", 300)),
        order_notification_interval_seconds=max(60, _int("ORDER_NOTIFICATION_INTERVAL_SECONDS", 300)),
        heartbeat_interval_seconds=max(15, _int("HEARTBEAT_INTERVAL_SECONDS", 30)),
        heartbeat_max_age_seconds=max(60, _int("HEARTBEAT_MAX_AGE_SECONDS", 120)),
        request_timeout_seconds=max(3, _int("REQUEST_TIMEOUT_SECONDS", 10)),
        referral_batch_size=max(1, _int("REFERRAL_BATCH_SIZE", 5)),
        referral_batch_points=max(1, _int("REFERRAL_BATCH_POINTS", 2)),
        default_contest_duration_minutes=max(1, _int("DEFAULT_CONTEST_DURATION_MINUTES", 1440)),
        image_render_concurrency=max(1, min(4, _int("IMAGE_RENDER_CONCURRENCY", 2))),
        signal_tracking_interval_seconds=max(60, _int("SIGNAL_TRACKING_INTERVAL_SECONDS", 60)),
        broadcast_batch_size=max(1, min(100, _int("BROADCAST_BATCH_SIZE", 25))),
        broadcast_job_interval_seconds=max(5, _int("BROADCAST_JOB_INTERVAL_SECONDS", 15)),
        privacy_retention_days=max(30, _int("PRIVACY_RETENTION_DAYS", 365)),
        privacy_delete_grace_days=max(1, _int("PRIVACY_DELETE_GRACE_DAYS", 7)),
        health_host=_first_env("HEALTH_HOST", default="0.0.0.0"),  # nosec B104
        health_port=_health_port(),
        external_backup_url=_url("EXTERNAL_BACKUP_URL"),
        backup_encryption_key=_first_env("BACKUP_ENCRYPTION_KEY"),
        sensitive_data_key=_first_env("SENSITIVE_DATA_KEY"),
        backup_key_version=_first_env("BACKUP_KEY_VERSION", default="v1"),
        backup_previous_keys=_strings("BACKUP_PREVIOUS_KEYS"),
        redis_url=_first_env("REDIS_URL"),
        concurrent_updates=max(1, min(64, _int("CONCURRENT_UPDATES", 8))),
        usdt_quote_valid_minutes=max(5, min(1440, _int("USDT_QUOTE_VALID_MINUTES", 30))),
        usdt_verification_enabled=_first_env("USDT_VERIFICATION_ENABLED", default="false").lower() in {"1", "true", "yes", "on"},
        usdt_trongrid_api_key=_first_env("TRONGRID_API_KEY"),
        usdt_tron_api_url=_url("TRON_API_URL") or "https://apilist.tronscanapi.com",
        usdt_ethereum_rpc_url=_url("ETHEREUM_RPC_URL"),
        usdt_bsc_rpc_url=_url("BSC_RPC_URL"),
        gemini_enabled=_truthy_env("GEMINI_ENABLED", False),
        gemini_api_key=_first_env("GEMINI_API_KEY"),
        gemini_model=_first_env("GEMINI_MODEL", default="gemini-3.5-flash"),
        gemini_api_base_url=_url("GEMINI_API_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta",
        gemini_timeout_seconds=max(5, min(120, _int("GEMINI_TIMEOUT_SECONDS", 30))),
        gemini_max_retries=max(0, min(5, _int("GEMINI_MAX_RETRIES", 2))),
        gemini_user_assistant_enabled=_truthy_env("GEMINI_USER_ASSISTANT_ENABLED", True),
        gemini_admin_copilot_enabled=_truthy_env("GEMINI_ADMIN_COPILOT_ENABLED", True),
        gemini_crm_enabled=_truthy_env("GEMINI_CRM_ENABLED", True),
        gemini_channel_control_enabled=_truthy_env("GEMINI_CHANNEL_CONTROL_ENABLED", True),
        gemini_user_behavior_enabled=_truthy_env("GEMINI_USER_BEHAVIOR_ENABLED", True),
        gemini_ticket_assistant_enabled=_truthy_env("GEMINI_TICKET_ASSISTANT_ENABLED", True),
        gemini_signal_analysis_enabled=_truthy_env("GEMINI_SIGNAL_ANALYSIS_ENABLED", True),
        gemini_risk_supervision_enabled=_truthy_env("GEMINI_RISK_SUPERVISION_ENABLED", True),
        gemini_system_monitoring_enabled=_truthy_env("GEMINI_SYSTEM_MONITORING_ENABLED", True),
        gemini_user_daily_request_limit=max(1, min(100, _int("GEMINI_USER_DAILY_REQUEST_LIMIT", 5))),
        gemini_admin_daily_request_limit=max(1, min(500, _int("GEMINI_ADMIN_DAILY_REQUEST_LIMIT", 50))),
        gemini_daily_token_limit=max(1000, min(100000000, _int("GEMINI_DAILY_TOKEN_LIMIT", 1000000))),
        gemini_monthly_token_limit=max(10000, min(1000000000, _int("GEMINI_MONTHLY_TOKEN_LIMIT", 20000000))),
        gemini_data_retention_days=max(1, min(3650, _int("GEMINI_DATA_RETENTION_DAYS", 90))),
        gemini_context_max_chars=max(4000, min(250000, _int("GEMINI_CONTEXT_MAX_CHARS", 60000))),
        gemini_healthcheck_ttl_seconds=max(30, min(3600, _int("GEMINI_HEALTHCHECK_TTL_SECONDS", 300))),
        gemini_user_review_cache_seconds=max(0, min(86400, _int("GEMINI_USER_REVIEW_CACHE_SECONDS", 600))),
        gemini_daily_report_enabled=_truthy_env("GEMINI_DAILY_REPORT_ENABLED", False),
        gemini_daily_report_hour=max(0, min(23, _int("GEMINI_DAILY_REPORT_HOUR", 8))),
        gemini_pseudonym_key=_first_env("GEMINI_PSEUDONYM_KEY", "SENSITIVE_DATA_KEY", "WEBAPP_SESSION_SECRET"),
        gemini_context_cache_seconds=max(0, min(3600, _int("GEMINI_CONTEXT_CACHE_SECONDS", 300))),
        gemini_autonomous_primary_admin=_truthy_env("GEMINI_AUTONOMOUS_PRIMARY_ADMIN", True),
        gemini_primary_admin_full_bot_access=_truthy_env("GEMINI_PRIMARY_ADMIN_FULL_BOT_ACCESS", False),
        gemini_delegated_admin_approval_required=_truthy_env("GEMINI_DELEGATED_ADMIN_APPROVAL_REQUIRED", True),
        gemini_max_tool_calls_per_request=max(1, min(50, _int("GEMINI_MAX_TOOL_CALLS_PER_REQUEST", 20))),
        gemini_safety_threshold=_first_env("GEMINI_SAFETY_THRESHOLD", default="BLOCK_MEDIUM_AND_ABOVE").upper(),
        gemini_confirm_sensitive_actions=_truthy_env("GEMINI_CONFIRM_SENSITIVE_ACTIONS", True),
        gemini_voice_commands_enabled=_truthy_env("GEMINI_VOICE_COMMANDS_ENABLED", True),
        gemini_voice_model=_first_env("GEMINI_VOICE_MODEL", default="gemini-3.5-flash"),
        gemini_voice_max_seconds=max(5, min(600, _int("GEMINI_VOICE_MAX_SECONDS", 120))),
        gemini_voice_max_bytes=max(1024, min(50_000_000, _int("GEMINI_VOICE_MAX_BYTES", 10_485_760))),
        gemini_image_enabled=_truthy_env("GEMINI_IMAGE_ENABLED", True),
        gemini_image_model=_first_env("GEMINI_IMAGE_MODEL", default="gemini-3.1-flash-image"),
        gemini_image_fallback_model=_first_env("GEMINI_IMAGE_FALLBACK_MODEL", default="gemini-3.1-flash-lite-image"),
        gemini_generated_ads_dir=_first_env("GEMINI_GENERATED_ADS_DIR", default=_persistent_default("generated_ads")),
        webapp_api_enabled=_first_env("WEBAPP_API_ENABLED", default="true").lower() in {"1", "true", "yes", "on"},
        webapp_session_secret=_first_env("WEBAPP_SESSION_SECRET"),
        session_key_version=_first_env("WEBAPP_SESSION_KEY_VERSION", default="v1"),
        session_previous_keys=_strings("WEBAPP_SESSION_PREVIOUS_KEYS"),
        run_mode=_first_env("RUN_MODE", default="webhook" if _url("WEBHOOK_URL") else "polling").lower(),
        webhook_url=_url("WEBHOOK_URL"),
        # Binding on all interfaces is required behind the Railway/Docker reverse proxy.
        webhook_listen=_first_env("WEBHOOK_LISTEN", default="0.0.0.0"),  # nosec B104
        webhook_port=max(1, min(65535, _int("WEBHOOK_PORT", 8443, "PORT"))),
        webhook_path=_first_env("WEBHOOK_PATH", default="telegram").strip("/"),
        webhook_secret_token=_first_env("WEBHOOK_SECRET_TOKEN"),
        webhook_max_connections=max(1, min(100, _int("WEBHOOK_MAX_CONNECTIONS", 40))),
        sentry_dsn=_first_env("SENTRY_DSN"),
        sentry_environment=_first_env("SENTRY_ENVIRONMENT", default="production"),
        sentry_traces_sample_rate=max(0.0, min(1.0, _float("SENTRY_TRACES_SAMPLE_RATE", 0.05))),
        release_version=(BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() if (BASE_DIR / "VERSION").is_file() else "unknown",
        release_build_number=_first_env("RELEASE_BUILD_NUMBER"),
        release_commit_sha=_first_env("RELEASE_COMMIT_SHA", "GITHUB_SHA", "RAILWAY_GIT_COMMIT_SHA"),
        release_image_digest=_first_env("RELEASE_IMAGE_DIGEST"),
    )


SETTINGS = load_settings()


def is_primary_admin(user_id: int | str) -> bool:
    try:
        return int(user_id) == SETTINGS.primary_admin_id
    except (TypeError, ValueError):
        return False


def is_admin(user_id: int | str) -> bool:
    """Return True only for the single configured primary owner.

    Additional administrators must be stored in the delegated-admin registry and
    approved by the primary owner before they receive any administrative access.
    """
    return is_primary_admin(user_id)
