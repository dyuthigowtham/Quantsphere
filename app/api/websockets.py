import asyncio

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.models.database import AsyncSessionLocal, User
from app.services.auth_tokens import decode_access_token
from app.services.market_data import PriceCache

router = APIRouter()


class ConnectionManager:
    """
    Purpose:    Track connected price-stream WebSocket clients without using
                module-level global state — an instance lives on
                app.state.ws_manager for the lifetime of the app.
    """

    def __init__(self) -> None:
        """
        Purpose:    Initialize with no connected clients.
        Args:       None.
        Returns:    None.
        Raises:     None.
        """
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        """
        Purpose:    Accept and register a new WebSocket client.
        Args:       websocket (WebSocket): The incoming connection.
        Returns:    None.
        Raises:     None.
        """
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        """
        Purpose:    Deregister a WebSocket client that has disconnected.
        Args:       websocket (WebSocket): The connection to remove.
        Returns:    None.
        Raises:     None.
        """
        self._connections.discard(websocket)

    async def broadcast(self, message: dict) -> None:
        """
        Purpose:    Send a JSON message to every currently connected client.
        Args:       message (dict): Payload to broadcast, e.g. a price snapshot.
        Returns:    None.
        Raises:     None.
        """
        for connection in list(self._connections):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)


@router.websocket("/ws/prices")
async def stream_prices(websocket: WebSocket) -> None:
    """
    Purpose:    Push periodic in-memory price snapshots to a connected client.
                Populated once a live Alpaca/Polygon.io feed writes into the
                shared PriceCache; until then this streams an empty snapshot.
    Args:       websocket (WebSocket): The client connection.
    Returns:    None.
    Raises:     None.
    """
    manager: ConnectionManager = websocket.app.state.ws_manager
    price_cache: PriceCache = websocket.app.state.price_cache

    await manager.connect(websocket)
    try:
        while True:
            await websocket.send_json(await price_cache.snapshot())
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        manager.disconnect(websocket)


class AlertConnectionManager:
    """
    Purpose:    Track connected Smart Alert WebSocket clients keyed by
                user_id — deliberately separate from ConnectionManager
                (/ws/prices), which broadcasts identically to every
                connection with no user identity attached. Alerts are
                private per-user data; a shared broadcast channel (or
                client-side filtering of one) would leak one user's
                revenge-trading/oversizing alerts to every other connected
                browser.
    """

    def __init__(self) -> None:
        self._by_user: dict[int, set[WebSocket]] = {}

    async def connect(self, user_id: int, websocket: WebSocket) -> None:
        """
        Purpose:    Accept and register a client under its authenticated user_id.
        Args:       user_id (int): The connection's authenticated owner.
                    websocket (WebSocket): The incoming connection.
        Returns:    None.
        Raises:     None.
        """
        await websocket.accept()
        self._by_user.setdefault(user_id, set()).add(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket) -> None:
        """
        Purpose:    Deregister a client that has disconnected.
        Args:       user_id (int): The connection's authenticated owner.
                    websocket (WebSocket): The connection to remove.
        Returns:    None.
        Raises:     None.
        """
        connections = self._by_user.get(user_id)
        if connections:
            connections.discard(websocket)
            if not connections:
                del self._by_user[user_id]

    async def send_to_user(self, user_id: int, message: dict) -> None:
        """
        Purpose:    Push one alert to every connection this specific user
                    currently has open — never to any other user's connections.
        Args:       user_id (int): The alert's owning user.
                    message (dict): The alert payload (an AlertRead, serialized).
        Returns:    None.
        Raises:     None.
        """
        for connection in list(self._by_user.get(user_id, ())):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(user_id, connection)


@router.websocket("/ws/alerts")
async def stream_alerts(websocket: WebSocket, token: str = Query(...)) -> None:
    """
    Purpose:    Authenticated, per-user push channel for Smart Alerts. A
                native browser WebSocket can't set an Authorization header,
                so the bearer token travels as a query param instead,
                decoded with the same decode_access_token every HTTP route
                uses. Sends nothing itself — alerts.py/routes.py push
                messages via AlertConnectionManager.send_to_user() as real
                events occur; this coroutine just holds the connection open.
    Args:       websocket (WebSocket): The client connection.
                token (str): The bearer token, as a query parameter.
    Returns:    None.
    Raises:     None. Closes the connection (code 1008) rather than raising
                    if the token is invalid/expired or no longer refers to a real user.
    """
    try:
        user_id = decode_access_token(token)
    except jwt.PyJWTError:
        await websocket.close(code=1008)
        return

    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
    if user is None:
        await websocket.close(code=1008)
        return

    manager = websocket.app.state.alert_ws_manager
    await manager.connect(user_id, websocket)
    try:
        while True:
            # This channel is push-only; just keep the connection alive and
            # drop anything a client sends (there's nothing for it to say).
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
