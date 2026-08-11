import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pwdlib import PasswordHash
from pydantic import BaseModel, Field, model_validator

from novofon import (
    CALL_API_URL,
    NovofonAPIError,
    NovofonClient,
    call_session_id,
    event_idempotency_key,
    event_type,
    link_hash,
    normalize_employee_id,
    nested,
    normalize_phone,
    recording_url,
)

DATABASE_URL = os.environ["DATABASE_URL"]
# The browser credential is an opaque, server-side session. It is not a JWT
# and JavaScript never receives it.
SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "12"))
RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "/recordings")).resolve()
ADMIN_EMAIL = os.environ.get("CRM_ADMIN_EMAIL", "admin@mo54.local").lower()
ADMIN_PASSWORD = os.environ.get("CRM_ADMIN_PASSWORD", "")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")

# Public production values belong in .env on the VPS.  The defaults deliberately
# keep a developer's loopback stack usable without weakening public deployment.
PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN", "").rstrip("/")
SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "crm_session")
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}
if "SESSION_COOKIE_SECURE" not in os.environ:
    SESSION_COOKIE_SECURE = PUBLIC_ORIGIN.startswith("https://")
SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "strict").lower()
if SESSION_COOKIE_SAMESITE not in {"strict", "lax"}:
    raise RuntimeError("SESSION_COOKIE_SAMESITE must be strict or lax")
if PUBLIC_ORIGIN:
    public_url = urlparse(PUBLIC_ORIGIN)
    if public_url.scheme != "https" or not public_url.netloc or public_url.path not in {"", "/"}:
        raise RuntimeError("PUBLIC_ORIGIN must be an HTTPS origin without a path")
    if not SESSION_COOKIE_SECURE:
        raise RuntimeError("SESSION_COOKIE_SECURE must be true for an HTTPS PUBLIC_ORIGIN")
LOGIN_RATE_LIMIT = int(os.environ.get("CRM_LOGIN_MAX_ATTEMPTS", os.environ.get("CRM_LOGIN_RATE_LIMIT", "5")))
LOGIN_RATE_WINDOW_SEC = int(os.environ.get("CRM_LOGIN_WINDOW_SECONDS", os.environ.get("CRM_LOGIN_RATE_WINDOW_SEC", "900")))

NOVOFON_ACCESS_TOKEN = os.environ.get("NOVOFON_ACCESS_TOKEN", "")
NOVOFON_VIRTUAL_NUMBER = normalize_phone(os.environ.get("NOVOFON_VIRTUAL_NUMBER", "")) or ""
NOVOFON_CALL_API_URL = os.environ.get("NOVOFON_CALL_API_URL", CALL_API_URL)
NOVOFON_WEBHOOK_SECRET = os.environ.get("NOVOFON_WEBHOOK_SECRET", "")
NOVOFON_WEBHOOK_PATH_SECRET = os.environ.get("NOVOFON_WEBHOOK_PATH_SECRET", "")
NOVOFON_WEBHOOK_ALLOWED_IPS = {
    value.strip() for value in os.environ.get("NOVOFON_WEBHOOK_ALLOWED_IPS", "37.139.38.215").split(",") if value.strip()
}
NOVOFON_RECORDING_ALLOWED_HOSTS = {
    value.strip().lower()
    for value in os.environ.get("NOVOFON_RECORDING_ALLOWED_HOSTS", "novofon.ru").split(",")
    if value.strip()
}
if NOVOFON_ACCESS_TOKEN and not NOVOFON_CALL_API_URL.startswith("https://"):
    raise RuntimeError("NOVOFON_CALL_API_URL must use HTTPS")
if NOVOFON_ACCESS_TOKEN and not NOVOFON_VIRTUAL_NUMBER:
    raise RuntimeError("NOVOFON_VIRTUAL_NUMBER must be configured with Call API access")
if NOVOFON_WEBHOOK_SECRET or NOVOFON_WEBHOOK_PATH_SECRET:
    if (
        len(NOVOFON_WEBHOOK_SECRET) < 32
        or len(NOVOFON_WEBHOOK_PATH_SECRET) < 32
        or hmac.compare_digest(NOVOFON_WEBHOOK_SECRET, NOVOFON_WEBHOOK_PATH_SECRET)
    ):
        raise RuntimeError("Novofon webhook secrets must be different and at least 32 characters")
    if not NOVOFON_VIRTUAL_NUMBER:
        raise RuntimeError("NOVOFON_VIRTUAL_NUMBER must be configured with Novofon webhook secrets")

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False, kwargs={"row_factory": dict_row})
passwords = PasswordHash.recommended()
_login_attempts: dict[str, deque[float]] = defaultdict(deque)
_login_lock = threading.Lock()


def fetch_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetch_all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def execute(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone() if cur.description else None
        conn.commit()
        return row


def session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_server_session(user_id: UUID) -> tuple[str, datetime]:
    """Create an opaque session whose server-side row can be revoked."""

    token = secrets.token_urlsafe(48)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)
    execute(
        """INSERT INTO crm_sessions(user_id, token_hash, expires_at)
           VALUES(%s,%s,%s)""",
        (user_id, session_token_hash(token), expires),
    )
    return token, expires


def replace_user_sessions(user_id: UUID) -> tuple[str, datetime]:
    """Invalidate every prior browser session and issue exactly one fresh one."""

    token = secrets.token_urlsafe(48)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE crm_sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL", (user_id,))
        cur.execute(
            """INSERT INTO crm_sessions(user_id, token_hash, expires_at)
               VALUES(%s,%s,%s)""",
            (user_id, session_token_hash(token), expires),
        )
        conn.commit()
    return token, expires


def revoke_server_session(token: str | None) -> None:
    if not token:
        return
    execute(
        """UPDATE crm_sessions SET revoked_at=now()
           WHERE token_hash=%s AND revoked_at IS NULL""",
        (session_token_hash(token),),
    )


def set_session_cookie(response: Response, token: str, expires: datetime) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_HOURS * 3600,
        expires=expires,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite=SESSION_COOKIE_SAMESITE,
        path="/",
    )


def reserve_novofon_initiation(
    *,
    user_id: UUID,
    mapping_id: UUID,
    contact_id: UUID | None,
    deal_id: UUID | None,
    from_number: str,
    to_number: str,
    idempotency_key: str,
) -> tuple[dict[str, Any], bool]:
    """Durably claim one callback intent before any Call API request.

    The pair of transaction advisory locks serializes both an identical browser
    key and a rapid second tap for the same manager/number.  The network call is
    deliberately outside this transaction: once the `requested` row commits,
    every concurrent request sees it and returns the same intent instead of
    placing a second callback.
    """

    locks = sorted({f"novofon:idem:{idempotency_key}", f"novofon:rapid:{user_id}:{to_number}"})
    request_id = uuid4()
    with pool.connection() as conn, conn.cursor() as cur:
        for lock in locks:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock,))
        cur.execute(
            """SELECT id, call_session_id, call_id, status, requested_at, last_error
               FROM call_initiation_requests
               WHERE source='novofon' AND idempotency_key=%s""",
            (idempotency_key,),
        )
        existing = cur.fetchone()
        if existing:
            conn.commit()
            return dict(existing), True
        cur.execute(
            """SELECT id, call_session_id, call_id, status, requested_at, last_error
               FROM call_initiation_requests
               WHERE source='novofon' AND requested_by_user_id=%s AND to_number=%s
                 AND status IN ('requested','accepted','unknown')
                 AND requested_at > now() - interval '30 seconds'
               ORDER BY requested_at DESC LIMIT 1""",
            (user_id, to_number),
        )
        recent = cur.fetchone()
        if recent:
            conn.commit()
            return dict(recent), True
        cur.execute(
            """INSERT INTO call_initiation_requests(
                   id, source, provider, idempotency_key, provider_request_id,
                   requested_by_user_id, employee_mapping_id, contact_id, deal_id,
                   from_number, to_number, status, intent
               ) VALUES(%s,'novofon','novofon',%s,%s,%s,%s,%s,%s,%s,%s,'requested',%s::jsonb)
               RETURNING id, status, call_session_id, call_id""",
            (
                request_id,
                idempotency_key,
                str(request_id),
                user_id,
                mapping_id,
                contact_id,
                deal_id,
                from_number,
                to_number,
                json.dumps({"created_from": "crm", "contact_id": str(contact_id or "")}, ensure_ascii=False),
            ),
        )
        created = dict(cur.fetchone())
        conn.commit()
    return created, False


def bootstrap_admin() -> None:
    if not ADMIN_PASSWORD:
        raise RuntimeError("CRM_ADMIN_PASSWORD must be set")
    execute(
        """INSERT INTO crm_users(email, display_name, password_hash, role)
           VALUES(%s, 'Администратор', %s, 'admin')
           ON CONFLICT(email) DO NOTHING""",
        (ADMIN_EMAIL, passwords.hash(ADMIN_PASSWORD)),
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    pool.open()
    bootstrap_admin()
    yield
    pool.close()


app = FastAPI(title="MO54 Calls CRM", version="1.1.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory="static"), name="assets")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


class User(BaseModel):
    id: UUID
    email: str
    display_name: str
    role: Literal["admin", "manager"]
    must_change_password: bool = False


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=500)
    new_password: str = Field(min_length=16, max_length=500)


class CallPatch(BaseModel):
    contact_id: UUID | None = None
    owner_id: UUID | None = None
    contact_name: str | None = Field(None, max_length=200)
    contact_email: str | None = Field(None, max_length=320)
    contact_notes: str | None = None


class IngestCall(BaseModel):
    source: str = Field(default="asterisk", pattern=r"^[a-z0-9_-]{1,40}$")
    external_call_id: str = Field(min_length=1, max_length=200)
    direction: Literal["in", "out"]
    caller_number: str | None = None
    callee_number: str | None = None
    started_at: datetime
    duration_sec: int = Field(gt=0)
    recording_customer: str
    recording_manager: str


class DealCreate(BaseModel):
    contact_id: UUID
    call_id: int | None = None
    title: str = Field(min_length=1, max_length=300)
    stage: Literal["new", "qualified", "proposal", "negotiation", "won", "lost"] = "new"
    amount: float | None = Field(None, ge=0)
    probability: int | None = Field(None, ge=0, le=100)


class DealPatch(BaseModel):
    stage: Literal["new", "qualified", "proposal", "negotiation", "won", "lost"] | None = None
    amount: float | None = Field(None, ge=0)
    probability: int | None = Field(None, ge=0, le=100)
    loss_reason: str | None = None


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    due_at: datetime | None = None
    contact_id: UUID | None = None
    deal_id: UUID | None = None
    call_id: int | None = None
    assignee_id: UUID | None = None


class TaskPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=300)
    description: str | None = None
    due_at: datetime | None = None
    assignee_id: UUID | None = None
    status: Literal["open", "completed", "cancelled"] | None = None


class CallInitiate(BaseModel):
    contact_id: UUID | None = None
    phone: str | None = Field(None, max_length=50)
    deal_id: UUID | None = None
    idempotency_key: str | None = Field(None, min_length=8, max_length=200)

    @model_validator(mode="after")
    def contact_or_phone(self):
        if not self.contact_id and not self.phone:
            raise ValueError("Нужен контакт или номер телефона")
        return self


class EmployeeMappingUpsert(BaseModel):
    crm_user_id: UUID
    provider_employee_id: str = Field(min_length=1, max_length=100)
    mobile_phone: str = Field(min_length=7, max_length=50)
    provider_extension: str | None = Field(None, max_length=100)
    display_name: str | None = Field(None, max_length=200)

    @model_validator(mode="after")
    def numeric_provider_employee_id(self):
        canonical = normalize_employee_id(self.provider_employee_id)
        if canonical is None:
            raise ValueError("ID сотрудника Novofon должен быть положительным числом")
        self.provider_employee_id = str(canonical)
        return self


def remote_ip(request: Request) -> str:
    # The API is loopback-only in production; nginx sets this value after it has
    # received the TCP connection from Novofon.  It is also useful in local tests.
    return (request.headers.get("x-real-ip") or (request.client.host if request.client else "")).split(",")[0].strip()


def allowed_origins(request: Request) -> set[str]:
    if PUBLIC_ORIGIN:
        return {PUBLIC_ORIGIN}
    return {str(request.base_url).rstrip("/")}


def require_same_origin(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    origin = request.headers.get("origin", "").rstrip("/")
    if not origin or origin not in allowed_origins(request):
        raise HTTPException(403, "Запрос отклонён: неверный Origin")


def _prune_attempts(ip: str, now: float) -> deque[float]:
    attempts = _login_attempts[ip]
    while attempts and attempts[0] <= now - LOGIN_RATE_WINDOW_SEC:
        attempts.popleft()
    return attempts


def enforce_login_rate_limit(request: Request) -> None:
    now = time.monotonic()
    with _login_lock:
        attempts = _prune_attempts(remote_ip(request), now)
        if len(attempts) >= LOGIN_RATE_LIMIT:
            retry = max(1, int(LOGIN_RATE_WINDOW_SEC - (now - attempts[0])))
            raise HTTPException(429, "Слишком много попыток входа. Попробуйте позже.", headers={"Retry-After": str(retry)})


def record_login_failure(request: Request) -> None:
    with _login_lock:
        _prune_attempts(remote_ip(request), time.monotonic()).append(time.monotonic())


def clear_login_failures(request: Request) -> None:
    with _login_lock:
        _login_attempts.pop(remote_ip(request), None)


def authenticated_user(request: Request) -> User:
    # Deliberately accept only the HttpOnly cookie. A bearer token copied from
    # localStorage was the old model and must not remain a bypass around logout
    # or a mandatory password change.
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(401, "Недействительная сессия")
    row = fetch_one(
        """SELECT u.id, u.email, u.display_name, u.role::text AS role, u.must_change_password
           FROM crm_sessions s
           JOIN crm_users u ON u.id=s.user_id
           WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at > now()
             AND u.active""",
        (session_token_hash(token),),
    )
    if not row:
        raise HTTPException(401, "Пользователь отключён")
    return User(**row)


def current_user(request: Request, user: User = Depends(authenticated_user)) -> User:
    require_same_origin(request)
    if user.must_change_password:
        raise HTTPException(428, "Сначала смените стартовый пароль")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Требуются права администратора")
    return user


_PRIVATE_PROVIDER_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "integration_key",
        "recording_url",
        "file_link",
        "file_url",
        "record_file_link",
        "full_record_file_link",
    }
)


def redact_private_data(value: Any) -> Any:
    """Keep secrets and provider recording URLs out of audit/API projections."""

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, nested_value in value.items():
            if key == "raw_json":
                # It can contain a complete Novofon event, including a signed
                # recording link. Internal worker storage must never become a
                # generic UI/audit projection.
                redacted[key] = {"redacted": True}
            elif key.lower() in _PRIVATE_PROVIDER_KEYS:
                redacted[key] = "[redacted]"
            else:
                redacted[key] = redact_private_data(nested_value)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_private_data(item) for item in value]
    return value


def audit(user: User, entity_type: str, entity_id: str, action: str, before: Any, after: Any) -> None:
    execute(
        """INSERT INTO audit_log(actor_id, entity_type, entity_id, action, before_data, after_data)
           VALUES(%s,%s,%s,%s,%s::jsonb,%s::jsonb)""",
        (
            user.id,
            entity_type,
            entity_id,
            action,
            json.dumps(redact_private_data(before), default=str),
            json.dumps(redact_private_data(after), default=str),
        ),
    )


def require_call_access(call: dict[str, Any], user: User) -> None:
    if user.role == "admin" or str(call.get("owner_id") or "") == str(user.id):
        return
    raise HTTPException(403, "Нет доступа к данным этого звонка")


def require_contact_access(contact_id: UUID, user: User) -> None:
    if user.role == "admin":
        return
    row = fetch_one(
        """SELECT 1
           WHERE EXISTS(SELECT 1 FROM calls WHERE contact_id=%s AND owner_id=%s)
              OR EXISTS(SELECT 1 FROM deals WHERE contact_id=%s AND owner_id=%s)
              OR EXISTS(SELECT 1 FROM tasks WHERE contact_id=%s AND assignee_id=%s)""",
        (contact_id, user.id, contact_id, user.id, contact_id, user.id),
    )
    if not row:
        raise HTTPException(404, "Контакт не найден")


def safe_provider_recording_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return None
    for allowed in NOVOFON_RECORDING_ALLOWED_HOSTS:
        normalized = allowed.lstrip(".")
        if host == normalized or host.endswith("." + normalized):
            return url
    return None


def provider_recording_view(call_id: int) -> list[dict[str, Any]]:
    return fetch_all(
        """SELECT id, channel, duration_sec, provider_status, available_at, expires_at,
                  created_at, CASE WHEN recording_url IS NULL THEN false ELSE true END AS available
           FROM provider_recordings
           WHERE call_id=%s ORDER BY created_at DESC""",
        (call_id,),
    )


def initial_password_change_message() -> str:
    return "Сначала смените стартовый пароль"


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/health")
def health():
    fetch_one("SELECT 1")
    return {"status": "ok"}


@app.post("/api/auth/token")
def login(request: Request, response: Response, form: OAuth2PasswordRequestForm = Depends()):
    require_same_origin(request)
    enforce_login_rate_limit(request)
    row = fetch_one("SELECT * FROM crm_users WHERE lower(email)=lower(%s) AND active", (form.username,))
    if not row or not passwords.verify(form.password, row["password_hash"]):
        record_login_failure(request)
        raise HTTPException(401, "Неверный логин или пароль")
    clear_login_failures(request)
    token, expires = new_server_session(row["id"])
    set_session_cookie(response, token, expires)
    # Deliberately do not return the bearer value.  Browser JavaScript never sees it.
    return {"authenticated": True, "expires_at": expires, "must_change_password": bool(row.get("must_change_password", True))}


@app.post("/api/auth/logout", status_code=204)
def logout(request: Request, response: Response):
    require_same_origin(request)
    revoke_server_session(request.cookies.get(SESSION_COOKIE_NAME))
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.status_code = 204
    return response


@app.post("/api/auth/change-password")
def change_password(request: Request, response: Response, body: PasswordChange, user: User = Depends(authenticated_user)):
    require_same_origin(request)
    row = fetch_one("SELECT password_hash FROM crm_users WHERE id=%s AND active", (user.id,))
    if not row or not passwords.verify(body.current_password, row["password_hash"]):
        raise HTTPException(400, "Текущий пароль введён неверно")
    if passwords.verify(body.new_password, row["password_hash"]):
        raise HTTPException(400, "Новый пароль должен отличаться от текущего")
    # Changing a password invalidates every browser which had the old one,
    # including the just-completed bootstrap password flow. The response receives
    # one newly created server-side session, so the current manager stays signed in.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE crm_users SET password_hash=%s, must_change_password=false, updated_at=now() WHERE id=%s",
            (passwords.hash(body.new_password), user.id),
        )
        cur.execute("UPDATE crm_sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL", (user.id,))
        token = secrets.token_urlsafe(48)
        expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)
        cur.execute(
            "INSERT INTO crm_sessions(user_id, token_hash, expires_at) VALUES(%s,%s,%s)",
            (user.id, session_token_hash(token), expires),
        )
        conn.commit()
    set_session_cookie(response, token, expires)
    audit(user, "user", str(user.id), "change_password", None, {"must_change_password": False})
    return {"changed": True}


@app.get("/api/me")
def me(user: User = Depends(authenticated_user)):
    return user


@app.post("/api/ingest/calls")
def ingest_call(body: IngestCall, x_ingest_token: str | None = Header(None)):
    if not INGEST_TOKEN or not x_ingest_token or not hmac.compare_digest(x_ingest_token, INGEST_TOKEN):
        raise HTTPException(401, "Недействительный ingest token")
    if body.source != "asterisk":
        raise HTTPException(422, "Этот технический ingest принимает только source=asterisk")
    row = execute(
        """SELECT ingest_asterisk_call(%s,%s,%s,%s,%s,%s,%s,%s) AS id""",
        (
            body.external_call_id,
            body.direction,
            body.caller_number,
            body.callee_number,
            body.started_at,
            body.duration_sec,
            body.recording_customer,
            body.recording_manager,
        ),
    )
    return {"call_id": row["id"], "idempotency_key": f"{body.source}:{body.external_call_id}"}


DEFAULT_NOVOFON_ACCOUNT_TIMEZONE = "Europe/Moscow"


def novofon_account_timezone() -> ZoneInfo:
    configured = os.environ.get(
        "NOVOFON_ACCOUNT_TIMEZONE", DEFAULT_NOVOFON_ACCOUNT_TIMEZONE
    ).strip()
    try:
        return ZoneInfo(configured)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_NOVOFON_ACCOUNT_TIMEZONE)


def parse_event_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        # Novofon sends its date/time fields without an offset.  Its account
        # and reports use Moscow time, so do not silently treat those values
        # as UTC.
        return result if result.tzinfo else result.replace(tzinfo=novofon_account_timezone())
    except ValueError:
        return None


@app.post("/api/integrations/novofon/{path_secret}/events", status_code=204)
def novofon_event(path_secret: str, body: dict[str, Any], request: Request):
    # Defense in depth: nginx has the same allow-list and the high entropy URL
    # never appears in the normal CRM UI or API documentation.
    if not NOVOFON_WEBHOOK_PATH_SECRET or not hmac.compare_digest(path_secret, NOVOFON_WEBHOOK_PATH_SECRET):
        raise HTTPException(404, "Not found")
    if remote_ip(request) not in NOVOFON_WEBHOOK_ALLOWED_IPS:
        raise HTTPException(403, "Webhook source is not allowed")
    supplied_key = str(body.get("integration_key") or "")
    if not NOVOFON_WEBHOOK_SECRET or not supplied_key or not hmac.compare_digest(supplied_key, NOVOFON_WEBHOOK_SECRET):
        raise HTTPException(403, "Webhook integration key is invalid")
    event_number = normalize_phone(
        body.get("virtual_phone_number")
        or body.get("communication_number")
        or nested(body, "contact_info", "communication_number")
    )
    if event_number != NOVOFON_VIRTUAL_NUMBER:
        # A provider-side filter should already prevent this. Missing is also
        # unsafe: this CRM pilot owns exactly one Novofon line, so acknowledge
        # unrelated/partial events without creating cards or callback tasks.
        return Response(status_code=204)

    raw_payload = dict(body)
    raw_payload.pop("integration_key", None)
    url = recording_url(raw_payload)
    key = event_idempotency_key(raw_payload)
    execute(
        """INSERT INTO provider_events(
               source, provider, idempotency_key, external_event_id, event_type,
               call_session_id, recording_url, recording_link_hash, occurred_at,
               payload, signature_valid, processing_status
           ) VALUES(
               'novofon','novofon',%s,%s,%s,%s,%s,%s,%s,%s::jsonb,true,'received'
           ) ON CONFLICT DO NOTHING""",
        (
            key,
            str(raw_payload.get("delivery_id") or raw_payload.get("event_id") or raw_payload.get("notification_id") or "") or None,
            event_type(raw_payload),
            call_session_id(raw_payload),
            url,
            link_hash(url),
            parse_event_datetime(raw_payload.get("notification_time")),
            json.dumps(raw_payload, ensure_ascii=False, default=str),
        ),
    )
    # No CRM interpretation or provider API work happens in the webhook request.
    return Response(status_code=204)


@app.get("/api/dashboard")
def dashboard(user: User = Depends(current_user)):
    scope = "TRUE" if user.role == "admin" else "c.owner_id=%s"
    params: tuple[Any, ...] = () if user.role == "admin" else (user.id,)
    return fetch_one(
        f"""SELECT
           count(*) FILTER (WHERE c.started_at >= date_trunc('day', now()))::int AS calls_today,
           count(*) FILTER (WHERE c.processing_status='ready'
             AND c.started_at >= date_trunc('day', now()))::int AS ready_today,
           (SELECT count(*)::int FROM tasks WHERE status='open' AND due_at < now()
             {'AND assignee_id=%s' if user.role != 'admin' else ''}) AS overdue_tasks,
           (SELECT count(*)::int FROM tasks WHERE status='open'
             AND due_at >= date_trunc('day', now()) AND due_at < date_trunc('day', now()) + interval '1 day'
             {'AND assignee_id=%s' if user.role != 'admin' else ''}) AS tasks_today,
           (SELECT count(*)::int FROM deals WHERE stage='won'
             {'AND owner_id=%s' if user.role != 'admin' else ''}) AS won_deals,
           (SELECT count(*)::int FROM deals WHERE stage='lost'
             {'AND owner_id=%s' if user.role != 'admin' else ''}) AS lost_deals
           FROM calls c WHERE {scope}""",
        params * (5 if user.role != "admin" else 1),
    )


@app.get("/api/calls")
def list_calls(
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
):
    where: list[str] = ["TRUE"]
    params: list[Any] = []
    if user.role != "admin":
        where.append("c.owner_id=%s")
        params.append(user.id)
    if status:
        where.append("c.processing_status::text=%s")
        params.append(status)
    if q:
        where.append("(coalesce(ct.full_name,'') ILIKE %s OR ct.phone_normalized ILIKE %s OR c.call_id ILIKE %s)")
        params.extend([f"%{q}%"] * 3)
    params.extend([limit, offset])
    return fetch_all(
        f"""SELECT c.id, c.call_id, c.source, c.direction, c.started_at, c.duration_sec,
                   c.processing_status::text, c.processing_error,
                   ct.id AS contact_id, ct.full_name AS contact_name, ct.phone_normalized,
                   ci.data->>'summary' AS summary,
                   EXISTS(SELECT 1 FROM tasks t WHERE t.call_id=c.id AND t.status='open') AS has_open_task,
                   EXISTS(SELECT 1 FROM provider_recordings pr WHERE pr.call_id=c.id AND pr.recording_url IS NOT NULL)
                     AS has_provider_recording
            FROM calls c LEFT JOIN contacts ct ON ct.id=c.contact_id
            LEFT JOIN call_insights ci ON ci.call_id=c.id AND ci.is_current
            WHERE {' AND '.join(where)}
            ORDER BY c.started_at DESC LIMIT %s OFFSET %s""",
        tuple(params),
    )


@app.get("/api/calls/{call_id}")
def get_call(call_id: int, user: User = Depends(current_user)):
    call = fetch_one(
        """SELECT c.id, c.call_id, c.source, c.external_call_id, c.direction,
                  c.caller_number, c.callee_number, c.contact_id, c.owner_id,
                  c.started_at, c.ended_at, c.duration_sec, c.answered,
                  c.processing_status::text AS processing_status, c.processing_error,
                  c.status, c.created_at, c.updated_at,
                  to_jsonb(ct.*) AS contact,
                  to_jsonb(u.*) - 'password_hash' AS owner
           FROM calls c LEFT JOIN contacts ct ON ct.id=c.contact_id
           LEFT JOIN crm_users u ON u.id=c.owner_id WHERE c.id=%s""",
        (call_id,),
    )
    if not call:
        raise HTTPException(404, "Звонок не найден")
    require_call_access(call, user)
    call["recordings"] = fetch_all(
        "SELECT id, channel, mime_type, duration_sec, size_bytes, sha256, retention_until FROM recordings WHERE call_id=%s",
        (call_id,),
    )
    call["provider_recordings"] = provider_recording_view(call_id)
    call["transcript"] = fetch_one(
        "SELECT id, version, text, asr_model, language, created_at FROM transcripts WHERE call_id=%s ORDER BY version DESC LIMIT 1",
        (call_id,),
    )
    call["insight"] = fetch_one(
        "SELECT id, version, prompt_version, model, data, confidence, created_at FROM call_insights WHERE call_id=%s AND is_current",
        (call_id,),
    )
    call["deals"] = fetch_all(
        """SELECT d.*, d.stage::text AS stage FROM deals d JOIN call_deals cd ON cd.deal_id=d.id
           WHERE cd.call_id=%s ORDER BY d.created_at DESC""",
        (call_id,),
    )
    call["tasks"] = fetch_all(
        "SELECT *, status::text AS status FROM tasks WHERE call_id=%s ORDER BY due_at NULLS LAST",
        (call_id,),
    )
    call["automation"] = {
        "transcript": "unavailable" if call["source"] == "novofon" and not call["transcript"] else "ready" if call["transcript"] else "waiting",
        "analysis": "unavailable" if call["source"] == "novofon" and not call["transcript"] else "ready" if call["insight"] else "waiting",
        "reason": "В пилоте Novofon расшифровка и ИИ-анализ не подключены." if call["source"] == "novofon" and not call["transcript"] else None,
    }
    return call


@app.patch("/api/calls/{call_id}")
def patch_call(call_id: int, body: CallPatch, user: User = Depends(current_user)):
    before = fetch_one("SELECT * FROM calls WHERE id=%s", (call_id,))
    if not before:
        raise HTTPException(404, "Звонок не найден")
    require_call_access(before, user)
    if body.owner_id is not None:
        if user.role != "admin":
            raise HTTPException(403, "Назначать ответственного может только администратор")
        target_owner = fetch_one("SELECT id FROM crm_users WHERE id=%s AND active", (body.owner_id,))
        if not target_owner:
            raise HTTPException(422, "Нельзя назначить отключённого пользователя")
    contact_id = body.contact_id or before["contact_id"]
    if body.contact_id is not None:
        target_contact = fetch_one("SELECT id FROM contacts WHERE id=%s", (body.contact_id,))
        if not target_contact:
            raise HTTPException(404, "Контакт не найден")
        if user.role != "admin":
            require_contact_access(body.contact_id, user)
    if contact_id and any(value is not None for value in (body.contact_name, body.contact_email, body.contact_notes)):
        if user.role != "admin":
            require_contact_access(contact_id, user)
        execute(
            """UPDATE contacts SET full_name=coalesce(%s,full_name), email=coalesce(%s,email),
               notes=coalesce(%s,notes), updated_at=now() WHERE id=%s RETURNING id""",
            (body.contact_name, body.contact_email, body.contact_notes, contact_id),
        )
    after = execute(
        """UPDATE calls SET contact_id=coalesce(%s,contact_id), owner_id=coalesce(%s,owner_id),
           updated_at=now() WHERE id=%s
           RETURNING id, call_id, source, contact_id, owner_id, updated_at""",
        (body.contact_id, body.owner_id, call_id),
    )
    audit(user, "call", str(call_id), "update", before, after)
    return after


@app.post("/api/calls/{call_id}/retry")
def retry_call(call_id: int, stage: Literal["transcribe", "analyze"] = "transcribe", user: User = Depends(current_user)):
    call = fetch_one("SELECT id, source, owner_id FROM calls WHERE id=%s", (call_id,))
    if not call:
        raise HTTPException(404, "Звонок не найден")
    require_call_access(call, user)
    if call["source"] == "novofon":
        raise HTTPException(409, "В пилоте Novofon автоматическая расшифровка и ИИ-анализ не подключены")
    execute(
        """INSERT INTO processing_jobs(call_id,kind,status,next_attempt_at,attempts,last_error)
           VALUES(%s,%s,'pending',now(),0,NULL)
           ON CONFLICT(call_id,kind) DO UPDATE SET status='pending',next_attempt_at=now(),
             attempts=0,last_error=NULL,locked_at=NULL,locked_by=NULL,updated_at=now()
           RETURNING id""",
        (call_id, stage),
    )
    execute(
        "UPDATE calls SET processing_status=%s, processing_error=NULL, updated_at=now() WHERE id=%s",
        ("recorded" if stage == "transcribe" else "analyzing", call_id),
    )
    audit(user, "call", str(call_id), f"retry_{stage}", None, {"stage": stage})
    return {"queued": True}


@app.get("/api/recordings/{recording_id}/open")
def open_provider_recording(recording_id: UUID, user: User = Depends(current_user)):
    row = fetch_one(
        """SELECT pr.recording_url, pr.call_id, c.owner_id
           FROM provider_recordings pr JOIN calls c ON c.id=pr.call_id WHERE pr.id=%s""",
        (recording_id,),
    )
    if not row:
        raise HTTPException(404, "Запись провайдера не найдена")
    require_call_access(row, user)
    target = safe_provider_recording_url(row["recording_url"])
    if not target:
        raise HTTPException(404, "Ссылка на запись недоступна")
    response = RedirectResponse(target, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/api/recordings/{recording_id}")
def recording(recording_id: UUID, user: User = Depends(current_user)):
    row = fetch_one(
        """SELECT r.storage_path, r.mime_type, c.owner_id
           FROM recordings r JOIN calls c ON c.id=r.call_id WHERE r.id=%s""",
        (recording_id,),
    )
    if not row:
        raise HTTPException(404, "Запись не найдена")
    require_call_access(row, user)
    target = Path(row["storage_path"]).resolve()
    if RECORDINGS_DIR not in target.parents or not target.is_file():
        raise HTTPException(404, "Файл записи недоступен")
    return FileResponse(target, media_type=row["mime_type"], filename=target.name)


@app.get("/api/contacts")
def contacts(q: str | None = None, user: User = Depends(current_user)):
    needle = f"%{q or ''}%"
    scope = "TRUE" if user.role == "admin" else "EXISTS(SELECT 1 FROM calls sc WHERE sc.contact_id=ct.id AND sc.owner_id=%s)"
    params: tuple[Any, ...] = (needle, needle) if user.role == "admin" else (needle, needle, user.id)
    return fetch_all(
        f"""SELECT ct.*, count(c.id)::int AS calls_count, max(c.started_at) AS last_call_at
           FROM contacts ct LEFT JOIN calls c ON c.contact_id=ct.id
           WHERE (coalesce(ct.full_name,'') ILIKE %s OR ct.phone_normalized ILIKE %s) AND {scope}
           GROUP BY ct.id ORDER BY last_call_at DESC NULLS LAST LIMIT 200""",
        params,
    )


@app.get("/api/contacts/{contact_id}")
def contact(contact_id: UUID, user: User = Depends(current_user)):
    require_contact_access(contact_id, user)
    row = fetch_one("SELECT * FROM contacts WHERE id=%s", (contact_id,))
    if not row:
        raise HTTPException(404, "Контакт не найден")
    owner_clause = "" if user.role == "admin" else " AND owner_id=%s"
    owner_params: tuple[Any, ...] = (contact_id,) if user.role == "admin" else (contact_id, user.id)
    row["calls"] = fetch_all(
        f"""SELECT id, call_id, source, direction, started_at, duration_sec, processing_status::text
            FROM calls WHERE contact_id=%s{owner_clause} ORDER BY started_at DESC""",
        owner_params,
    )
    row["deals"] = fetch_all(
        "SELECT *, stage::text AS stage FROM deals WHERE contact_id=%s" + ("" if user.role == "admin" else " AND owner_id=%s") + " ORDER BY created_at DESC",
        (contact_id,) if user.role == "admin" else (contact_id, user.id),
    )
    row["tasks"] = fetch_all(
        "SELECT *, status::text AS status FROM tasks WHERE contact_id=%s" + ("" if user.role == "admin" else " AND assignee_id=%s") + " ORDER BY due_at NULLS LAST",
        (contact_id,) if user.role == "admin" else (contact_id, user.id),
    )
    return row


@app.post("/api/calls/initiate")
async def initiate_call(
    body: CallInitiate,
    user: User = Depends(current_user),
    idempotency_header: str | None = Header(None, alias="Idempotency-Key"),
):
    contact: dict[str, Any] | None = None
    if body.contact_id:
        require_contact_access(body.contact_id, user)
        contact = fetch_one("SELECT id, phone_normalized FROM contacts WHERE id=%s", (body.contact_id,))
        if not contact:
            raise HTTPException(404, "Контакт не найден")
    phone = normalize_phone(body.phone or (contact or {}).get("phone_normalized"))
    if not phone:
        raise HTTPException(422, "Укажите корректный номер клиента")
    if body.deal_id:
        deal = fetch_one("SELECT id, contact_id, owner_id FROM deals WHERE id=%s", (body.deal_id,))
        if not deal or (user.role != "admin" and str(deal["owner_id"]) != str(user.id)):
            raise HTTPException(404, "Сделка не найдена")
        if contact and str(deal["contact_id"]) != str(contact["id"]):
            raise HTTPException(422, "Сделка относится к другому контакту")
    mapping = fetch_one(
        """SELECT pem.*, pem.provider_payload->>'mobile_phone' AS mobile_phone
           FROM provider_employee_mappings pem
           WHERE pem.provider='novofon' AND pem.crm_user_id=%s AND pem.active
           ORDER BY pem.updated_at DESC LIMIT 1""",
        (user.id,),
    )
    if not mapping:
        raise HTTPException(409, "Администратор ещё не связал ваш аккаунт с сотрудником Novofon")
    manager_phone = normalize_phone(mapping.get("mobile_phone"))
    if not manager_phone:
        raise HTTPException(409, "В настройке сотрудника Novofon нет мобильного номера")
    if not NOVOFON_ACCESS_TOKEN or not NOVOFON_VIRTUAL_NUMBER:
        raise HTTPException(503, "Call API Novofon ещё не настроен на сервере")

    supplied_idempotency_key = (idempotency_header or body.idempotency_key or str(uuid4())).strip()
    if len(supplied_idempotency_key) > 200:
        raise HTTPException(422, "Idempotency-Key слишком длинный")
    # Scope a browser key to its authenticated user, avoiding cross-user
    # idempotency collisions while retaining a fixed-size database key.
    idempotency_key = "crm:" + hashlib.sha256(
        f"{user.id}:{supplied_idempotency_key}".encode("utf-8")
    ).hexdigest()
    reserved, duplicate = reserve_novofon_initiation(
        user_id=user.id,
        mapping_id=mapping["id"],
        contact_id=(contact or {}).get("id"),
        deal_id=body.deal_id,
        from_number=NOVOFON_VIRTUAL_NUMBER,
        to_number=phone,
        idempotency_key=idempotency_key,
    )
    if duplicate:
        return {
            "id": reserved["id"],
            "status": reserved["status"],
            "call_session_id": reserved["call_session_id"],
            "call_id": reserved["call_id"],
            "duplicate": True,
        }
    request_id = reserved["id"]
    client = NovofonClient(NOVOFON_ACCESS_TOKEN, NOVOFON_VIRTUAL_NUMBER, NOVOFON_CALL_API_URL)
    try:
        result = await client.start_employee_call(
            request_id=str(request_id),
            employee_id=mapping["provider_employee_id"],
            employee_phone=manager_phone,
            contact_phone=phone,
        )
    except NovofonAPIError as exc:
        # Network uncertainty is not retried: Novofon might already be calling
        # the manager.  Events/reconciliation will resolve the final outcome.
        execute(
            """UPDATE call_initiation_requests SET status='unknown', last_error=%s,
               updated_at=now() WHERE id=%s""",
            (str(exc)[:1000], request_id),
        )
        raise HTTPException(502, "Не удалось подтвердить исходящий callback. Не нажимайте повторно: проверьте журнал звонков.")

    provider_result = result["result"]
    provider_data = provider_result.get("data") if isinstance(provider_result.get("data"), dict) else provider_result
    session = str(provider_data.get("call_session_id") or provider_result.get("call_session_id") or "") or None
    request_log = dict(result["request"])
    request_params = dict(request_log.get("params") or {})
    request_params["access_token"] = "[redacted]"
    request_log["params"] = request_params
    row = execute(
        """UPDATE call_initiation_requests
           SET status='accepted', call_session_id=%s, provider_status=%s,
               provider_request=%s::jsonb, provider_response=%s::jsonb, accepted_at=now(), updated_at=now()
           WHERE id=%s
           RETURNING id, status, call_session_id, call_id""",
        (
            session,
            str(provider_result.get("status") or "accepted"),
            json.dumps(request_log, ensure_ascii=False),
            json.dumps(result["response"], ensure_ascii=False),
            request_id,
        ),
    )
    audit(user, "call_initiation", str(request_id), "create", None, {"to_number": phone, "status": row["status"]})
    return {"id": row["id"], "status": row["status"], "call_session_id": row["call_session_id"], "call_id": row["call_id"], "duplicate": False}


@app.get("/api/calls/initiations/{request_id}")
def call_initiation(request_id: UUID, user: User = Depends(current_user)):
    row = fetch_one(
        """SELECT id, requested_by_user_id, status, call_session_id, call_id, to_number,
                  requested_at, accepted_at, completed_at, last_error
           FROM call_initiation_requests WHERE id=%s""",
        (request_id,),
    )
    if not row or (user.role != "admin" and str(row["requested_by_user_id"]) != str(user.id)):
        raise HTTPException(404, "Исходящий вызов не найден")
    return row


@app.post("/api/deals")
def create_deal(body: DealCreate, user: User = Depends(current_user)):
    require_contact_access(body.contact_id, user)
    row = execute(
        """INSERT INTO deals(contact_id,owner_id,title,stage,amount,probability)
           VALUES(%s,%s,%s,%s,%s,%s) RETURNING *,stage::text AS stage""",
        (body.contact_id, user.id, body.title, body.stage, body.amount, body.probability),
    )
    if body.call_id:
        execute("INSERT INTO call_deals(call_id,deal_id) VALUES(%s,%s) ON CONFLICT DO NOTHING", (body.call_id, row["id"]))
    audit(user, "deal", str(row["id"]), "create", None, row)
    return row


@app.patch("/api/deals/{deal_id}")
def patch_deal(deal_id: UUID, body: DealPatch, user: User = Depends(current_user)):
    before = fetch_one("SELECT * FROM deals WHERE id=%s", (deal_id,))
    if not before or (user.role != "admin" and str(before["owner_id"]) != str(user.id)):
        raise HTTPException(404, "Сделка не найдена")
    values = body.model_dump(exclude_unset=True)
    if not values:
        return before
    assignments = [f"{key}=%s" for key in values]
    after = execute(
        f"UPDATE deals SET {','.join(assignments)},updated_at=now() WHERE id=%s RETURNING *,stage::text AS stage",
        tuple(values.values()) + (deal_id,),
    )
    audit(user, "deal", str(deal_id), "update", before, after)
    return after


@app.get("/api/deals")
def deals(user: User = Depends(current_user)):
    return fetch_all(
        """SELECT d.*,d.stage::text AS stage,ct.full_name AS contact_name,ct.phone_normalized
           FROM deals d JOIN contacts ct ON ct.id=d.contact_id""" + ("" if user.role == "admin" else " WHERE d.owner_id=%s") + " ORDER BY d.updated_at DESC",
        () if user.role == "admin" else (user.id,),
    )


@app.post("/api/tasks")
def create_task(body: TaskCreate, user: User = Depends(current_user)):
    if body.contact_id:
        require_contact_access(body.contact_id, user)
    row = execute(
        """INSERT INTO tasks(contact_id,deal_id,call_id,assignee_id,title,description,due_at)
           VALUES(%s,%s,%s,coalesce(%s,%s),%s,%s,%s) RETURNING *,status::text AS status""",
        (body.contact_id, body.deal_id, body.call_id, body.assignee_id, user.id, body.title, body.description, body.due_at),
    )
    audit(user, "task", str(row["id"]), "create", None, row)
    return row


@app.get("/api/tasks")
def tasks(status: Literal["open", "completed", "cancelled"] = "open", user: User = Depends(current_user)):
    return fetch_all(
        """SELECT t.*,t.status::text AS status,ct.full_name AS contact_name,ct.phone_normalized
           FROM tasks t LEFT JOIN contacts ct ON ct.id=t.contact_id
           WHERE t.status=%s""" + ("" if user.role == "admin" else " AND t.assignee_id=%s") + " ORDER BY t.due_at NULLS LAST,t.created_at",
        (status,) if user.role == "admin" else (status, user.id),
    )


@app.patch("/api/tasks/{task_id}")
def patch_task(task_id: UUID, body: TaskPatch, user: User = Depends(current_user)):
    before = fetch_one("SELECT * FROM tasks WHERE id=%s", (task_id,))
    if not before or (user.role != "admin" and str(before["assignee_id"]) != str(user.id)):
        raise HTTPException(404, "Задача не найдена")
    values = body.model_dump(exclude_unset=True)
    if not values:
        return before
    if values.get("status") == "completed":
        values["completed_at"] = datetime.now(timezone.utc)
    assignments = [f"{key}=%s" for key in values]
    after = execute(
        f"UPDATE tasks SET {','.join(assignments)},updated_at=now() WHERE id=%s RETURNING *,status::text AS status",
        tuple(values.values()) + (task_id,),
    )
    audit(user, "task", str(task_id), "update", before, after)
    return after


@app.get("/api/pipeline")
def pipeline(user: User = Depends(current_user)):
    return fetch_all(
        """SELECT stage::text AS stage,count(*)::int AS count,coalesce(sum(amount),0) AS amount
           FROM deals""" + ("" if user.role == "admin" else " WHERE owner_id=%s") + " GROUP BY stage ORDER BY array_position(ARRAY['new','qualified','proposal','negotiation','won','lost'],stage::text)",
        () if user.role == "admin" else (user.id,),
    )


@app.get("/api/admin/jobs")
def jobs(user: User = Depends(require_admin)):
    return fetch_all(
        """SELECT j.*,j.kind::text AS kind,j.status::text AS status,c.call_id
           FROM processing_jobs j JOIN calls c ON c.id=j.call_id
           WHERE j.status IN ('pending','running','retry','failed')
           ORDER BY j.updated_at DESC LIMIT 200"""
    )


@app.get("/api/admin/novofon/employee-mappings")
def employee_mappings(user: User = Depends(require_admin)):
    return fetch_all(
        """SELECT pem.id, pem.crm_user_id, cu.email, cu.display_name AS crm_user_name,
                  pem.provider_employee_id, pem.provider_extension, pem.display_name,
                  pem.provider_status, pem.active, pem.provider_payload->>'mobile_phone' AS mobile_phone,
                  pem.updated_at
           FROM provider_employee_mappings pem JOIN crm_users cu ON cu.id=pem.crm_user_id
           WHERE pem.provider='novofon' ORDER BY pem.active DESC, pem.updated_at DESC"""
    )


@app.put("/api/admin/novofon/employee-mappings")
def upsert_employee_mapping(body: EmployeeMappingUpsert, user: User = Depends(require_admin)):
    mobile_phone = normalize_phone(body.mobile_phone)
    if not mobile_phone:
        raise HTTPException(422, "Укажите корректный мобильный номер сотрудника")
    crm_user = fetch_one("SELECT id FROM crm_users WHERE id=%s AND active", (body.crm_user_id,))
    if not crm_user:
        raise HTTPException(404, "CRM-пользователь не найден")
    row = execute(
        """WITH deactivate AS (
               UPDATE provider_employee_mappings SET active=false, updated_at=now()
               WHERE provider='novofon' AND active
                 AND (crm_user_id=%s OR provider_employee_id=%s)
           )
           INSERT INTO provider_employee_mappings(
               provider, crm_user_id, provider_employee_id, provider_extension, display_name,
               active, provider_payload
           ) VALUES('novofon',%s,%s,%s,%s,true,%s::jsonb)
           ON CONFLICT(provider,crm_user_id,provider_employee_id) DO UPDATE SET
             provider_extension=excluded.provider_extension, display_name=excluded.display_name,
             active=true, provider_payload=excluded.provider_payload, updated_at=now()
           RETURNING id, crm_user_id, provider_employee_id, provider_extension, display_name,
                     active, provider_payload->>'mobile_phone' AS mobile_phone, updated_at""",
        (
            body.crm_user_id,
            body.provider_employee_id,
            body.crm_user_id,
            body.provider_employee_id,
            body.provider_extension,
            body.display_name,
            json.dumps({"mobile_phone": mobile_phone}),
        ),
    )
    audit(user, "provider_employee_mapping", str(row["id"]), "upsert", None, row)
    return row
