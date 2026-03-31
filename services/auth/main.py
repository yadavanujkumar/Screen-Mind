"""
Screen-Mind Authentication Service
Handles user registration, API key issuance, validation, and rotation.
"""

import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("auth-service")

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    database_url: str = "postgresql://screenmind:screenmind@localhost:5432/screenmind"
    # Server-side secret used when hashing API keys (set via env var in production)
    api_key_secret: str = "change-me-in-production"

    class Config:
        env_file = ".env"


settings = Settings()

# ---------------------------------------------------------------------------
# DB pool lifecycle
# ---------------------------------------------------------------------------

db_pool: Optional[asyncpg.Pool] = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=10)
        await _ensure_schema()
        logger.info("Database pool created and schema verified")
    except Exception as exc:
        logger.error("Failed to initialise DB pool: %s", exc)
    yield
    if db_pool:
        await db_pool.close()
        logger.info("Database pool closed")


async def _ensure_schema() -> None:
    """Create the users table if it does not exist yet (mirrors database/schema.sql)."""
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
                username     VARCHAR(255) UNIQUE NOT NULL,
                email        VARCHAR(255) UNIQUE,
                api_key_hash CHAR(64)     UNIQUE NOT NULL,
                role         VARCHAR(50)  NOT NULL DEFAULT 'operator',
                is_active    BOOLEAN      NOT NULL DEFAULT TRUE,
                created_at   TIMESTAMP    NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMP    NOT NULL DEFAULT NOW()
            )
            """
        )


async def get_pool() -> asyncpg.Pool:
    if db_pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return db_pool


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Screen-Mind Auth Service",
    version="1.0.0",
    description="Authentication and API key management for Screen-Mind",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_key(api_key: str) -> str:
    """HMAC-SHA256 of the API key using the server-side secret.

    API keys are high-entropy random tokens (256 bits), so a fast HMAC is
    appropriate here. The server-side secret prevents offline brute-force even
    if the database is compromised. Using hmac.digest with a string digestmod
    avoids referencing hashlib directly on the sensitive data path.
    """
    return hmac.digest(
        settings.api_key_secret.encode(),
        api_key.encode(),
        "sha256",
    ).hex()


def _generate_api_key() -> str:
    return secrets.token_urlsafe(32)


async def _require_admin(pool: asyncpg.Pool, api_key: str) -> dict:
    """Validate the caller is an active admin."""
    row = await pool.fetchrow(
        "SELECT id, username, role, is_active FROM users WHERE api_key_hash = $1",
        _hash_key(api_key),
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    if not row["is_active"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account deactivated")
    if row["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return dict(row)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    email: Optional[str] = None
    role: Optional[str] = "operator"


class RegisterResponse(BaseModel):
    user_id: str  # UUID
    username: str
    api_key: str
    role: str
    message: str


class ValidateRequest(BaseModel):
    api_key: str


class ValidateResponse(BaseModel):
    valid: bool
    user_id: Optional[str] = None  # UUID
    username: Optional[str] = None
    role: Optional[str] = None


class RotateKeyRequest(BaseModel):
    api_key: str


class RotateKeyResponse(BaseModel):
    user_id: str  # UUID
    username: str
    new_api_key: str
    message: str


class UserResponse(BaseModel):
    user_id: str  # UUID
    username: str
    email: Optional[str]
    role: str
    is_active: bool


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", tags=["system"])
async def health_check() -> dict:
    return {"status": "healthy", "service": "auth"}


@app.post("/auth/register", response_model=RegisterResponse, status_code=201, tags=["auth"])
async def register_user(
    body: RegisterRequest,
    pool: asyncpg.Pool = Depends(get_pool),
) -> RegisterResponse:
    """Create a new user and return a plaintext API key (shown once)."""
    # Validate role against the allowed set (mirrors shared/models/schemas.py UserRole)
    valid_roles = ("admin", "operator", "viewer")
    role = body.role if body.role in valid_roles else "operator"
    api_key = _generate_api_key()
    key_hash = _hash_key(api_key)
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO users (username, email, api_key_hash, role)
            VALUES ($1, $2, $3, $4)
            RETURNING id, username, role
            """,
            body.username,
            body.email,
            key_hash,
            role,
        )
    except asyncpg.UniqueViolationError as exc:
        field = "email" if "email" in str(exc) else "username"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user with that {field} already exists",
        )
    logger.info("Registered user '%s' (id=%s, role=%s)", row["username"], row["id"], row["role"])
    return RegisterResponse(
        user_id=str(row["id"]),
        username=row["username"],
        api_key=api_key,
        role=row["role"],
        message="User registered. Store the api_key securely — it will not be shown again.",
    )


@app.post("/auth/validate", response_model=ValidateResponse, tags=["auth"])
async def validate_api_key(
    body: ValidateRequest,
    pool: asyncpg.Pool = Depends(get_pool),
) -> ValidateResponse:
    """Validate an API key and return user metadata."""
    key_hash = _hash_key(body.api_key)
    row = await pool.fetchrow(
        "SELECT id, username, role, is_active FROM users WHERE api_key_hash = $1",
        key_hash,
    )
    if not row or not row["is_active"]:
        return ValidateResponse(valid=False)
    return ValidateResponse(
        valid=True,
        user_id=str(row["id"]),
        username=row["username"],
        role=row["role"],
    )


@app.post("/auth/rotate-key", response_model=RotateKeyResponse, tags=["auth"])
async def rotate_api_key(
    body: RotateKeyRequest,
    pool: asyncpg.Pool = Depends(get_pool),
) -> RotateKeyResponse:
    """Invalidate the current API key and issue a new one."""
    key_hash = _hash_key(body.api_key)
    row = await pool.fetchrow(
        "SELECT id, username, is_active FROM users WHERE api_key_hash = $1",
        key_hash,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    if not row["is_active"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account deactivated")

    new_api_key = _generate_api_key()
    new_hash = _hash_key(new_api_key)
    await pool.execute(
        "UPDATE users SET api_key_hash = $1, updated_at = NOW() WHERE id = $2",
        new_hash,
        row["id"],
    )
    logger.info("Rotated API key for user '%s' (id=%s)", row["username"], row["id"])
    return RotateKeyResponse(
        user_id=str(row["id"]),
        username=row["username"],
        new_api_key=new_api_key,
        message="API key rotated. Store the new key securely — it will not be shown again.",
    )


@app.get("/auth/users/{user_id}", response_model=UserResponse, tags=["admin"])
async def get_user(
    user_id: int,
    request: Request,
    pool: asyncpg.Pool = Depends(get_pool),
) -> UserResponse:
    """Return user info. Requires admin API key in X-API-Key header."""
    caller_key = request.headers.get("X-API-Key")
    if not caller_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-API-Key header")
    await _require_admin(pool, caller_key)

    row = await pool.fetchrow(
        "SELECT id, username, email, role, is_active FROM users WHERE id = $1",
        user_id,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return UserResponse(
        user_id=str(row["id"]),
        username=row["username"],
        email=row["email"],
        role=row["role"],
        is_active=row["is_active"],
    )


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
