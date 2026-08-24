from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import MT5Connection, MT5ConnectionStatus, Portfolio
from app.models.schemas import MT5ConnectRequest
from app.services.crypto import encrypt_password
from app.services.mt5_sync import MT5ConnectionError, MT5ConnectionManager, MT5SyncService


async def _get_connection(db: AsyncSession, portfolio_id: int) -> MT5Connection | None:
    """Fetch a portfolio's MT5Connection row, or None if never linked."""
    return (
        await db.execute(select(MT5Connection).where(MT5Connection.portfolio_id == portfolio_id))
    ).scalar_one_or_none()


async def connect_account(
    db: AsyncSession, mt5_manager: MT5ConnectionManager, portfolio_id: int, payload: MT5ConnectRequest
) -> MT5Connection:
    """
    Purpose:    Link a MetaTrader 5 account to a portfolio from the UI:
                validate the credentials with a real (throwaway) connection
                attempt before persisting anything, then store them
                (password encrypted) and start the live background sync loop.
    Args:       db (AsyncSession): The active database session.
                mt5_manager (MT5ConnectionManager): Shared sync-task registry.
                portfolio_id (int): Portfolio to link the account to.
                payload (MT5ConnectRequest): Validated login/password/server/terminal_path.
    Returns:    MT5Connection: The persisted, now-connected row.
    Raises:     HTTPException: 404 if the portfolio doesn't exist; 502 if the
                    credentials don't work (terminal unreachable or login rejected).
    """
    portfolio = await db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio not found")

    probe = MT5SyncService(payload.login, payload.password, payload.server, payload.terminal_path)
    try:
        await probe.connect()
    except MT5ConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    finally:
        await probe.disconnect()

    connection = await _get_connection(db, portfolio_id)
    encrypted = encrypt_password(payload.password)
    if connection is None:
        connection = MT5Connection(portfolio_id=portfolio_id, login=payload.login, encrypted_password=encrypted)
        db.add(connection)

    connection.login = payload.login
    connection.encrypted_password = encrypted
    connection.server = payload.server
    connection.terminal_path = payload.terminal_path
    connection.enabled = True
    connection.status = MT5ConnectionStatus.CONNECTED
    connection.last_error = None

    await db.commit()
    await db.refresh(connection)

    await mt5_manager.start(portfolio_id, payload.login, payload.password, payload.server, payload.terminal_path)
    return connection


async def disconnect_account(db: AsyncSession, mt5_manager: MT5ConnectionManager, portfolio_id: int) -> None:
    """
    Purpose:    Unlink a portfolio's MT5 account: stop its background sync
                loop and mark the stored connection disabled.
    Args:       db (AsyncSession): The active database session.
                mt5_manager (MT5ConnectionManager): Shared sync-task registry.
                portfolio_id (int): Portfolio whose account should be unlinked.
    Returns:    None.
    Raises:     HTTPException: 404 if no account is linked to this portfolio.
    """
    connection = await _get_connection(db, portfolio_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No MT5 account linked to this portfolio")

    await mt5_manager.stop(portfolio_id)
    connection.enabled = False
    connection.status = MT5ConnectionStatus.DISCONNECTED
    connection.last_error = None
    await db.commit()


async def get_status(db: AsyncSession, portfolio_id: int) -> MT5Connection | None:
    """
    Purpose:    Fetch a portfolio's current MT5 link status for display.
    Args:       db (AsyncSession): The active database session.
                portfolio_id (int): Portfolio to check.
    Returns:    MT5Connection | None: The connection row, or None if never linked.
    Raises:     None.
    """
    return await _get_connection(db, portfolio_id)
