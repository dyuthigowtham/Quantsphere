from collections.abc import AsyncGenerator

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.ollama_client import OllamaClient
from app.models.database import AsyncSessionLocal, User
from app.services.auth_tokens import decode_access_token
from app.services.market_data import PriceCache
from app.services.mt5_sync import MT5ConnectionManager
from app.services.news import NewsCache

_bearer_scheme = HTTPBearer()


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """
    Purpose:    FastAPI dependency yielding a per-request async database
                session, so no route ever holds or mutates a shared global session.
    Args:       request (Request): Unused directly; present so FastAPI resolves
                    this as a request-scoped dependency.
    Returns:    AsyncGenerator[AsyncSession, None]: Yields one session, closed after the request.
    Raises:     None.
    """
    async with AsyncSessionLocal() as session:
        yield session


def get_ollama_client() -> OllamaClient:
    """
    Purpose:    FastAPI dependency providing the Phase B local LLM mentor client.
    Args:       None.
    Returns:    OllamaClient: A client bound to settings.ollama_base_url.
    Raises:     None.
    """
    return OllamaClient()


def get_price_cache(request: Request) -> PriceCache:
    """
    Purpose:    FastAPI dependency providing the app-lifetime PriceCache
                instance stored on app.state (set up in the lifespan), rather
                than a module-level global.
    Args:       request (Request): Used to reach `request.app.state.price_cache`.
    Returns:    PriceCache: The shared in-memory price cache for this app instance.
    Raises:     None.
    """
    return request.app.state.price_cache


def get_mt5_manager(request: Request) -> MT5ConnectionManager:
    """
    Purpose:    FastAPI dependency providing the app-lifetime MT5ConnectionManager
                instance stored on app.state (set up in the lifespan), rather
                than a module-level global.
    Args:       request (Request): Used to reach `request.app.state.mt5_manager`.
    Returns:    MT5ConnectionManager: The shared registry of active MT5 sync tasks.
    Raises:     None.
    """
    return request.app.state.mt5_manager


def get_news_cache(request: Request) -> NewsCache:
    """
    Purpose:    FastAPI dependency providing the app-lifetime NewsCache
                instance stored on app.state (set up in the lifespan), rather
                than a module-level global.
    Args:       request (Request): Used to reach `request.app.state.news_cache`.
    Returns:    NewsCache: The shared, time-boxed RSS headline cache.
    Raises:     None.
    """
    return request.app.state.news_cache


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Purpose:    FastAPI dependency resolving the authenticated user from the
                request's bearer token — every route touching a user's own
                data (portfolio/trade/setup/strategy/etc.) depends on this.
    Args:       credentials (HTTPAuthorizationCredentials): The parsed
                    `Authorization: Bearer <token>` header.
                db (AsyncSession): The active database session.
    Returns:    User: The authenticated user row.
    Raises:     HTTPException: 401 if the token is missing, malformed,
                    expired, or no longer refers to an existing user.
    """
    try:
        user_id = decode_access_token(credentials.credentials)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token") from exc

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    return user
