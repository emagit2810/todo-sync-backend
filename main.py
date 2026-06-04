import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, validator


app = FastAPI(title="Todo Sync API", version="1.0.0")

DEFAULT_CORS_ORIGINS = {
    "https://todo-finalboss.onrender.com",
    "https://todo-celular-prototipo.onrender.com",
}


def get_cors_origins() -> List[str]:
    configured = {
        origin.strip()
        for origin in os.getenv("CORS_ALLOW_ORIGINS", "").split(",")
        if origin.strip()
    }
    if not configured:
        return ["*"]
    configured.update(DEFAULT_CORS_ORIGINS)
    return sorted(configured)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

bearer_scheme = HTTPBearer(auto_error=False)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
API_BEARER_TOKEN = os.getenv("API_BEARER_TOKEN", "").strip()
GOOGLE_CLIENT_ID = (
    os.getenv("GOOGLE_CLIENT_ID", "").strip()
    or os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
)
TODO_SYNC_ALLOWED_EMAILS = {
    item.strip().lower()
    for item in os.getenv("TODO_SYNC_ALLOWED_EMAILS", "").split(",")
    if item.strip()
}
TODO_SYNC_DEFAULT_USER_TOKEN = os.getenv("TODO_SYNC_DEFAULT_USER_TOKEN", "emmanuel_main").strip()
TODO_SYNC_DEFAULT_EMAIL = os.getenv("TODO_SYNC_DEFAULT_EMAIL", "").strip()
TODO_SYNC_HTTP_TIMEOUT_SEC = float(os.getenv("TODO_SYNC_HTTP_TIMEOUT_SEC", "8"))
TODO_SYNC_NOTE_WINDOW_DAYS = int(os.getenv("TODO_SYNC_NOTE_WINDOW_DAYS", "92"))
SYNC_MAX_ATTACHMENT_BASE64_CHARS = int(
    os.getenv("TODO_SYNC_MAX_ATTACHMENT_BASE64_CHARS", str(30 * 1024 * 1024))
)


class SyncUser(BaseModel):
    user_token: str
    email: Optional[str] = None
    name: Optional[str] = None
    provider: str = "google"


class SyncEntityOperationIn(BaseModel):
    op_id: Optional[str] = Field(None, max_length=160)
    entity_type: str = Field(..., min_length=1, max_length=64)
    entity_id: str = Field(..., min_length=1, max_length=160)
    action: str = Field(..., pattern=r"^(upsert|delete)$")
    data: Optional[Dict[str, Any]] = None
    client_updated_at: int = Field(default=0, ge=0)

    @validator("entity_type")
    def validate_entity_type(cls, value):
        if value not in {"task", "note", "note_folder", "expense", "medicine"}:
            raise ValueError("unsupported entity_type")
        return value


class SyncAttachmentOperationIn(BaseModel):
    op_id: Optional[str] = Field(None, max_length=160)
    attachment_id: str = Field(..., min_length=1, max_length=160)
    action: str = Field(..., pattern=r"^(upsert|delete)$")
    entity_type: Optional[str] = Field(None, max_length=64)
    entity_id: Optional[str] = Field(None, max_length=160)
    file_name: Optional[str] = Field(None, max_length=260)
    mime_type: Optional[str] = Field(default="application/octet-stream", max_length=160)
    size_bytes: int = Field(default=0, ge=0)
    data_base64: Optional[str] = None
    storage_provider: Optional[str] = Field(default=None, max_length=32)
    object_key: Optional[str] = Field(default=None, max_length=600)
    public_url: Optional[str] = Field(default=None, max_length=1200)
    client_updated_at: int = Field(default=0, ge=0)


class SyncImportIn(BaseModel):
    source: Optional[str] = Field(default="indexeddb-import", max_length=80)
    entities: List[SyncEntityOperationIn] = Field(default_factory=list)
    attachments: List[SyncAttachmentOperationIn] = Field(default_factory=list)

    @validator("entities")
    def validate_entities_size(cls, value):
        if len(value) > 5000:
            raise ValueError("entities import limit exceeded")
        return value

    @validator("attachments")
    def validate_attachments_size(cls, value):
        if len(value) > 1000:
            raise ValueError("attachments import limit exceeded")
        return value


class SyncPushIn(BaseModel):
    operations: List[SyncEntityOperationIn] = Field(default_factory=list)
    attachments: List[SyncAttachmentOperationIn] = Field(default_factory=list)

    @validator("operations")
    def validate_operations_size(cls, value):
        if len(value) > 1000:
            raise ValueError("operations limit exceeded")
        return value

    @validator("attachments")
    def validate_push_attachments_size(cls, value):
        if len(value) > 200:
            raise ValueError("attachment operations limit exceeded")
        return value


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_since(value: Optional[str]) -> str:
    if not value:
        return "1970-01-01T00:00:00Z"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid since cursor") from exc


def to_utc_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if value is None:
        return utc_now_iso()
    return str(value)


def record_timestamp_ms(data: Dict[str, Any]) -> int:
    for key in ("updatedAt", "createdAt", "date", "lastUpdated"):
        value = data.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return 0


def is_note_inside_sync_window(data: Dict[str, Any]) -> bool:
    record_ts = record_timestamp_ms(data)
    if record_ts <= 0:
        return False
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - TODO_SYNC_NOTE_WINDOW_DAYS * 24 * 60 * 60 * 1000
    return record_ts >= cutoff_ms


def pg_conn():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="Missing dependency: psycopg[binary]") from exc

    return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)


def jsonb(value: Dict[str, Any]):
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def ensure_schema() -> None:
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS app_users (
                    user_token TEXT PRIMARY KEY,
                    email TEXT,
                    name TEXT,
                    provider TEXT NOT NULL DEFAULT 'google',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_app_users_email "
                "ON app_users (LOWER(email)) WHERE email IS NOT NULL"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_items (
                    user_token TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    data JSONB NOT NULL DEFAULT '{}'::jsonb,
                    deleted BOOLEAN NOT NULL DEFAULT FALSE,
                    client_updated_at BIGINT NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    version BIGINT NOT NULL DEFAULT 1,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_token, entity_type, entity_id)
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sync_items_user_updated "
                "ON sync_items (user_token, updated_at)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_operations_dedupe (
                    user_token TEXT NOT NULL,
                    op_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_token, op_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_attachments (
                    user_token TEXT NOT NULL,
                    attachment_id TEXT NOT NULL,
                    entity_type TEXT,
                    entity_id TEXT,
                    file_name TEXT,
                    mime_type TEXT,
                    size_bytes BIGINT NOT NULL DEFAULT 0,
                    data_base64 TEXT,
                    storage_provider TEXT,
                    object_key TEXT,
                    public_url TEXT,
                    deleted BOOLEAN NOT NULL DEFAULT FALSE,
                    client_updated_at BIGINT NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_token, attachment_id)
                )
                """
            )
            cur.execute("ALTER TABLE sync_attachments ADD COLUMN IF NOT EXISTS storage_provider TEXT")
            cur.execute("ALTER TABLE sync_attachments ADD COLUMN IF NOT EXISTS object_key TEXT")
            cur.execute("ALTER TABLE sync_attachments ADD COLUMN IF NOT EXISTS public_url TEXT")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sync_attachments_user_updated "
                "ON sync_attachments (user_token, updated_at)"
            )


def upsert_sync_user(user: SyncUser) -> None:
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_users (user_token, email, name, provider, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (user_token)
                DO UPDATE SET
                    email = EXCLUDED.email,
                    name = EXCLUDED.name,
                    provider = EXCLUDED.provider,
                    updated_at = NOW()
                """,
                (user.user_token, user.email, user.name, user.provider),
            )


def resolve_google_user_token(email: str, google_user_token: str) -> str:
    """Reuse an existing manual-import account when Gmail ownership matches."""
    if not DATABASE_URL:
        return google_user_token

    ensure_schema()
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_token
                FROM app_users
                WHERE LOWER(email) = LOWER(%s)
                ORDER BY CASE WHEN user_token = %s THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (email, google_user_token),
            )
            row = cur.fetchone()
    return str(row["user_token"]) if row else google_user_token


async def verify_sync_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> SyncUser:
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing sync auth token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials.strip()
    if API_BEARER_TOKEN and token == API_BEARER_TOKEN:
        return SyncUser(
            user_token=TODO_SYNC_DEFAULT_USER_TOKEN or "emmanuel_main",
            email=TODO_SYNC_DEFAULT_EMAIL or None,
            name="Todo local migration",
            provider="backend-token",
        )

    if not GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google sync auth is not configured",
        )

    async with httpx.AsyncClient(timeout=TODO_SYNC_HTTP_TIMEOUT_SEC) as client:
        response = await client.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": token},
        )

    if response.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Google ID token")

    payload = response.json()
    audience = str(payload.get("aud") or "")
    if audience != GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Google token audience mismatch")

    email = str(payload.get("email") or "").strip().lower()
    email_verified = str(payload.get("email_verified") or "").lower() == "true"
    if not email or not email_verified:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Google email is not verified")

    if TODO_SYNC_ALLOWED_EMAILS and email not in TODO_SYNC_ALLOWED_EMAILS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Google account is not allowed for sync")

    subject = str(payload.get("sub") or "").strip()
    if not subject:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Google token has no subject")

    user_token = resolve_google_user_token(email, f"google:{subject}")
    return SyncUser(
        user_token=user_token,
        email=email,
        name=str(payload.get("name") or "").strip() or None,
        provider="google",
    )


def dedupe_operation(cur, user_token: str, op_id: Optional[str], entity_type: str, entity_id: str, action: str) -> bool:
    if not op_id:
        return True

    cur.execute(
        """
        INSERT INTO sync_operations_dedupe (user_token, op_id, entity_type, entity_id, action)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_token, op_id) DO NOTHING
        RETURNING op_id
        """,
        (user_token, op_id, entity_type, entity_id, action),
    )
    return cur.fetchone() is not None


def apply_entity_operation(cur, user_token: str, operation: SyncEntityOperationIn) -> bool:
    if not dedupe_operation(
        cur,
        user_token,
        operation.op_id,
        operation.entity_type,
        operation.entity_id,
        operation.action,
    ):
        return False

    if operation.action == "delete":
        data = {}
        deleted = True
    else:
        data = dict(operation.data or {})
        data["id"] = data.get("id") or operation.entity_id
        if operation.entity_type == "note" and not is_note_inside_sync_window(data):
            return False
        deleted = False

    cur.execute(
        """
        INSERT INTO sync_items (
            user_token, entity_type, entity_id, data, deleted, client_updated_at, updated_at, version
        )
        VALUES (%s, %s, %s, %s, %s, %s, NOW(), 1)
        ON CONFLICT (user_token, entity_type, entity_id)
        DO UPDATE SET
            data = EXCLUDED.data,
            deleted = EXCLUDED.deleted,
            client_updated_at = EXCLUDED.client_updated_at,
            updated_at = NOW(),
            version = sync_items.version + 1
        WHERE sync_items.client_updated_at <= EXCLUDED.client_updated_at
        """,
        (
            user_token,
            operation.entity_type,
            operation.entity_id,
            jsonb(data),
            deleted,
            operation.client_updated_at,
        ),
    )
    return True


def apply_attachment_operation(cur, user_token: str, operation: SyncAttachmentOperationIn) -> bool:
    if not dedupe_operation(
        cur,
        user_token,
        operation.op_id,
        "attachment",
        operation.attachment_id,
        operation.action,
    ):
        return False

    if operation.data_base64 and len(operation.data_base64) > SYNC_MAX_ATTACHMENT_BASE64_CHARS:
        raise HTTPException(status_code=413, detail=f"attachment_too_large:{operation.attachment_id}")

    deleted = operation.action == "delete"
    cur.execute(
        """
        INSERT INTO sync_attachments (
            user_token, attachment_id, entity_type, entity_id, file_name, mime_type,
            size_bytes, data_base64, storage_provider, object_key, public_url,
            deleted, client_updated_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (user_token, attachment_id)
        DO UPDATE SET
            entity_type = EXCLUDED.entity_type,
            entity_id = EXCLUDED.entity_id,
            file_name = EXCLUDED.file_name,
            mime_type = EXCLUDED.mime_type,
            size_bytes = EXCLUDED.size_bytes,
            data_base64 = EXCLUDED.data_base64,
            storage_provider = EXCLUDED.storage_provider,
            object_key = EXCLUDED.object_key,
            public_url = EXCLUDED.public_url,
            deleted = EXCLUDED.deleted,
            client_updated_at = EXCLUDED.client_updated_at,
            updated_at = NOW()
        WHERE sync_attachments.client_updated_at <= EXCLUDED.client_updated_at
        """,
        (
            user_token,
            operation.attachment_id,
            operation.entity_type,
            operation.entity_id,
            operation.file_name or operation.attachment_id,
            operation.mime_type or "application/octet-stream",
            operation.size_bytes,
            None if deleted else operation.data_base64,
            operation.storage_provider,
            operation.object_key,
            operation.public_url,
            deleted,
            operation.client_updated_at,
        ),
    )
    return True


def collect_sync_rows(cur, user_token: str, since_iso: str, entity_limit: int = 2000, attachment_limit: int = 500) -> Dict[str, Any]:
    max_cursor = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))

    cur.execute(
        """
        SELECT entity_type, entity_id, data, deleted, client_updated_at, updated_at
        FROM sync_items
        WHERE user_token = %s
          AND updated_at > %s::timestamptz
        ORDER BY updated_at ASC
        LIMIT %s
        """,
        (user_token, since_iso, entity_limit),
    )
    entities = []
    for row in cur.fetchall():
        row_updated_at = row["updated_at"].astimezone(timezone.utc)
        if row_updated_at > max_cursor:
            max_cursor = row_updated_at
        entities.append(
            {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "data": row["data"] or {},
                "deleted": bool(row["deleted"]),
                "client_updated_at": int(row["client_updated_at"] or 0),
                "updated_at": to_utc_iso(row["updated_at"]),
            }
        )

    cur.execute(
        """
        SELECT attachment_id, entity_type, entity_id, file_name, mime_type, size_bytes,
               data_base64, storage_provider, object_key, public_url,
               deleted, client_updated_at, updated_at
        FROM sync_attachments
        WHERE user_token = %s
          AND updated_at > %s::timestamptz
        ORDER BY updated_at ASC
        LIMIT %s
        """,
        (user_token, since_iso, attachment_limit),
    )
    attachments = []
    for row in cur.fetchall():
        row_updated_at = row["updated_at"].astimezone(timezone.utc)
        if row_updated_at > max_cursor:
            max_cursor = row_updated_at
        attachments.append(
            {
                "attachment_id": row["attachment_id"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "file_name": row["file_name"],
                "mime_type": row["mime_type"],
                "size_bytes": int(row["size_bytes"] or 0),
                "data_base64": None if row["deleted"] else row["data_base64"],
                "storage_provider": row["storage_provider"],
                "object_key": row["object_key"],
                "public_url": row["public_url"],
                "deleted": bool(row["deleted"]),
                "client_updated_at": int(row["client_updated_at"] or 0),
                "updated_at": to_utc_iso(row["updated_at"]),
            }
        )

    return {
        "entities": entities,
        "attachments": attachments,
        "next_since": max_cursor.isoformat().replace("+00:00", "Z"),
    }


@app.on_event("startup")
def startup_event() -> None:
    if DATABASE_URL:
        ensure_schema()


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "todo-sync-api", "server_time": utc_now_iso()}


@app.get("/v1/sync/schema")
def todo_sync_schema(user: SyncUser = Depends(verify_sync_user)):
    ensure_schema()
    upsert_sync_user(user)
    return {
        "ok": True,
        "user": {"email": user.email, "provider": user.provider},
        "tables": ["app_users", "sync_items", "sync_attachments", "sync_operations_dedupe"],
    }


@app.get("/auth/me")
def sync_auth_me(user: SyncUser = Depends(verify_sync_user)):
    return {
        "ok": True,
        "user": {"email": user.email, "name": user.name, "provider": user.provider},
    }


@app.post("/v1/sync/import")
def todo_sync_import(payload: SyncImportIn, user: SyncUser = Depends(verify_sync_user)):
    ensure_schema()
    upsert_sync_user(user)
    applied_entities = 0
    applied_attachments = 0
    with pg_conn() as conn:
        with conn.cursor() as cur:
            for operation in payload.entities:
                if apply_entity_operation(cur, user.user_token, operation):
                    applied_entities += 1
            for operation in payload.attachments:
                if apply_attachment_operation(cur, user.user_token, operation):
                    applied_attachments += 1
    return {
        "ok": True,
        "status": "imported",
        "source": payload.source,
        "entities": applied_entities,
        "attachments": applied_attachments,
        "server_time": utc_now_iso(),
    }


@app.post("/v1/sync/push")
def todo_sync_push(payload: SyncPushIn, user: SyncUser = Depends(verify_sync_user)):
    ensure_schema()
    upsert_sync_user(user)
    applied_entities = 0
    applied_attachments = 0
    with pg_conn() as conn:
        with conn.cursor() as cur:
            for operation in payload.operations:
                if apply_entity_operation(cur, user.user_token, operation):
                    applied_entities += 1
            for operation in payload.attachments:
                if apply_attachment_operation(cur, user.user_token, operation):
                    applied_attachments += 1
    return {
        "ok": True,
        "status": "pushed",
        "entities": applied_entities,
        "attachments": applied_attachments,
        "server_time": utc_now_iso(),
    }


@app.get("/v1/sync/pull")
def todo_sync_pull(since: Optional[str] = None, user: SyncUser = Depends(verify_sync_user)):
    ensure_schema()
    upsert_sync_user(user)
    since_iso = normalize_since(since)
    with pg_conn() as conn:
        with conn.cursor() as cur:
            result = collect_sync_rows(cur, user.user_token, since_iso)
    return {"ok": True, "server_time": utc_now_iso(), **result}


@app.get("/v1/sync/export")
def todo_sync_export(user: SyncUser = Depends(verify_sync_user)):
    ensure_schema()
    upsert_sync_user(user)
    with pg_conn() as conn:
        with conn.cursor() as cur:
            result = collect_sync_rows(
                cur,
                user.user_token,
                "1970-01-01T00:00:00Z",
                entity_limit=20000,
                attachment_limit=5000,
            )
    return {"ok": True, "server_time": utc_now_iso(), **result}
    
    
    
    # ==========================
# DEBUG ENDPOINTS
# ==========================

import logging

logger = logging.getLogger("debug")

@app.get("/debug-version")
def debug_version():
    logger.info("DEBUG_VERSION called")

    return {
        "ok": True,
        "build": "debug-v2",
        "service": "todo-sync-api",
        "server_time": utc_now_iso()
    }


@app.get("/debug-routes")
def debug_routes():
    logger.info("DEBUG_ROUTES called")

    routes = []

    for route in app.routes:
        routes.append({
            "path": getattr(route, "path", ""),
            "name": getattr(route, "name", ""),
            "methods": list(getattr(route, "methods", []))
        })

    return {
        "ok": True,
        "count": len(routes),
        "routes": routes
    }


@app.get("/debug-db")
def debug_db():
    logger.info("DEBUG_DB called")

    try:
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT NOW() as now, current_database() as db, current_user as usr"
                )
                row = cur.fetchone()

        return {
            "ok": True,
            "database": row
        }

    except Exception as e:
        logger.exception("DATABASE ERROR")

        return {
            "ok": False,
            "error": str(e)
        }
