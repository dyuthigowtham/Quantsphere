import bcrypt
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Alert, Portfolio, Setup, Strategy, User
from app.models.schemas import PortfolioCreate, SetupCreate, StrategyCreate, UserCreate


def _hash_password(password: str) -> str:
    """
    Purpose:    Hash a plaintext password for storage with bcrypt (random
                per-hash salt, deliberately slow).
    Args:       password (str): Plaintext password.
    Returns:    str: The bcrypt hash, as a UTF-8 string.
    Raises:     None.
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    """
    Purpose:    Check a plaintext password against a stored bcrypt hash.
    Args:       password (str): Plaintext password from a login attempt.
                hashed_password (str): The stored bcrypt hash.
    Returns:    bool: True if they match.
    Raises:     None.
    """
    return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))


async def create_user(db: AsyncSession, user_data: UserCreate) -> User:
    """
    Purpose:    Register a new QuantSphere account.
    Args:       db (AsyncSession): The active database session.
                user_data (UserCreate): Validated email/password payload.
    Returns:    User: The persisted user row.
    Raises:     HTTPException: 409 if the email is already registered.
    """
    user = User(email=user_data.email, hashed_password=_hash_password(user_data.password))
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered") from exc
    await db.refresh(user)
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User:
    """
    Purpose:    Verify a login attempt's credentials.
    Args:       db (AsyncSession): The active database session.
                email (str): The attempted email.
                password (str): The attempted plaintext password.
    Returns:    User: The authenticated user row.
    Raises:     HTTPException: 401 with a generic message on either an
                    unknown email or a wrong password — never reveal which.
    """
    stmt = select(User).where(User.email == email)
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None or not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
    return user


async def create_portfolio(db: AsyncSession, user_id: int, portfolio_data: PortfolioCreate) -> Portfolio:
    """
    Purpose:    Create a new paper-trading portfolio for an existing user.
    Args:       db (AsyncSession): The active database session.
                user_id (int): The authenticated owner — never client-supplied.
                portfolio_data (PortfolioCreate): Validated name/starting balance.
    Returns:    Portfolio: The persisted portfolio row, funded at its starting balance.
    Raises:     None.
    """
    portfolio = Portfolio(
        user_id=user_id,
        name=portfolio_data.name,
        starting_balance=portfolio_data.starting_balance,
        current_balance=portfolio_data.starting_balance,
    )
    db.add(portfolio)
    await db.commit()
    await db.refresh(portfolio)
    return portfolio


async def get_portfolio(db: AsyncSession, portfolio_id: int) -> Portfolio:
    """
    Purpose:    Fetch a portfolio's current state (e.g. live balance) for display.
    Args:       db (AsyncSession): The active database session.
                portfolio_id (int): Identifier of the portfolio to fetch.
    Returns:    Portfolio: The persisted portfolio row.
    Raises:     HTTPException: 404 if the portfolio doesn't exist.
    """
    portfolio = await db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio not found")
    return portfolio


async def get_owned_portfolio(db: AsyncSession, portfolio_id: int, user: User) -> Portfolio:
    """
    Purpose:    Fetch a portfolio, enforcing that it belongs to the
                authenticated user — the ownership check every
                portfolio-scoped route must use.
    Args:       db (AsyncSession): The active database session.
                portfolio_id (int): Identifier of the portfolio to fetch.
                user (User): The authenticated requester.
    Returns:    Portfolio: The persisted portfolio row.
    Raises:     HTTPException: 404 if the portfolio doesn't exist OR belongs
                    to a different user — deliberately the same status/detail
                    as "doesn't exist" so a guessed id never confirms it's real.
    """
    portfolio = await get_portfolio(db, portfolio_id)
    if portfolio.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio not found")
    return portfolio


async def get_portfolio_for_user(db: AsyncSession, user_id: int) -> Portfolio | None:
    """
    Purpose:    Fetch a user's portfolio at login, so the frontend can skip
                straight to the dashboard without a separate "list my
                portfolios" round trip. QuantSphere's onboarding flow only
                ever creates one portfolio per user today.
    Args:       db (AsyncSession): The active database session.
                user_id (int): The authenticated user.
    Returns:    Portfolio | None: The user's first portfolio, or None if
                    they haven't created one yet.
    Raises:     None.
    """
    stmt = select(Portfolio).where(Portfolio.user_id == user_id).order_by(Portfolio.created_at.asc())
    return (await db.execute(stmt)).scalars().first()


async def create_setup(db: AsyncSession, setup_data: SetupCreate) -> Setup:
    """
    Purpose:    Define a new named trading setup/strategy under a portfolio,
                so trades can be tagged with it for Trading DNA's setup-level
                performance breakdown.
    Args:       db (AsyncSession): The active database session.
                setup_data (SetupCreate): Validated portfolio/name/description.
    Returns:    Setup: The persisted setup row.
    Raises:     HTTPException: 404 if the portfolio doesn't exist; 409 if a
                    setup with that name already exists on the portfolio.
    """
    await get_portfolio(db, setup_data.portfolio_id)

    setup = Setup(
        portfolio_id=setup_data.portfolio_id,
        name=setup_data.name,
        description=setup_data.description,
    )
    db.add(setup)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A setup with that name already exists") from exc
    await db.refresh(setup)
    return setup


async def list_setups_for_portfolio(db: AsyncSession, portfolio_id: int) -> list[Setup]:
    """
    Purpose:    List every setup defined under a portfolio, for the setup
                picker on trade tagging and the "Check My Trade" form.
    Args:       db (AsyncSession): The active database session.
                portfolio_id (int): Portfolio whose setups should be listed.
    Returns:    list[Setup]: Setups, newest first.
    Raises:     None.
    """
    stmt = select(Setup).where(Setup.portfolio_id == portfolio_id).order_by(Setup.created_at.desc())
    return list((await db.execute(stmt)).scalars().all())


async def create_strategy(db: AsyncSession, strategy_data: StrategyCreate) -> Strategy:
    """
    Purpose:    Save a new Strategy Lab strategy definition. Never tagged
                onto real trades — a Strategy is a separate, simulated-only
                concept from a Setup.
    Args:       db (AsyncSession): The active database session.
                strategy_data (StrategyCreate): Validated name/direction/
                    conditions/risk parameters.
    Returns:    Strategy: The persisted strategy row.
    Raises:     HTTPException: 404 if the portfolio doesn't exist; 409 if a
                    strategy with that name already exists on the portfolio.
    """
    await get_portfolio(db, strategy_data.portfolio_id)

    strategy = Strategy(
        portfolio_id=strategy_data.portfolio_id,
        name=strategy_data.name,
        description=strategy_data.description,
        direction=strategy_data.direction,
        conditions=[c.model_dump() for c in strategy_data.conditions],
        stop_loss_pct=strategy_data.stop_loss_pct,
        target_r=strategy_data.target_r,
    )
    db.add(strategy)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A strategy with that name already exists") from exc
    await db.refresh(strategy)
    return strategy


async def list_strategies_for_portfolio(db: AsyncSession, portfolio_id: int) -> list[Strategy]:
    """
    Purpose:    List every strategy defined under a portfolio, for the
                Strategy Lab's strategy list/comparison view.
    Args:       db (AsyncSession): The active database session.
                portfolio_id (int): Portfolio whose strategies should be listed.
    Returns:    list[Strategy]: Strategies, newest first.
    Raises:     None.
    """
    stmt = select(Strategy).where(Strategy.portfolio_id == portfolio_id).order_by(Strategy.created_at.desc())
    return list((await db.execute(stmt)).scalars().all())


async def get_strategy(db: AsyncSession, strategy_id: int) -> Strategy:
    """
    Purpose:    Fetch one saved strategy, e.g. to run a backtest against it.
    Args:       db (AsyncSession): The active database session.
                strategy_id (int): Identifier of the strategy to fetch.
    Returns:    Strategy: The persisted strategy row.
    Raises:     HTTPException: 404 if the strategy doesn't exist.
    """
    strategy = await db.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    return strategy


async def get_owned_alert(db: AsyncSession, alert_id: int, user: User) -> Alert:
    """
    Purpose:    Fetch a Smart Alert, enforcing that its portfolio belongs to
                the authenticated user.
    Args:       db (AsyncSession): The active database session.
                alert_id (int): Identifier of the alert to fetch.
                user (User): The authenticated requester.
    Returns:    Alert: The persisted alert row.
    Raises:     HTTPException: 404 if the alert doesn't exist OR its
                    portfolio belongs to a different user.
    """
    alert = await db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    await get_owned_portfolio(db, alert.portfolio_id, user)
    return alert


async def get_owned_strategy(db: AsyncSession, strategy_id: int, user: User) -> Strategy:
    """
    Purpose:    Fetch a strategy, enforcing that its portfolio belongs to
                the authenticated user.
    Args:       db (AsyncSession): The active database session.
                strategy_id (int): Identifier of the strategy to fetch.
                user (User): The authenticated requester.
    Returns:    Strategy: The persisted strategy row.
    Raises:     HTTPException: 404 if the strategy doesn't exist OR its
                    portfolio belongs to a different user.
    """
    strategy = await get_strategy(db, strategy_id)
    await get_owned_portfolio(db, strategy.portfolio_id, user)
    return strategy


async def delete_strategy(db: AsyncSession, strategy_id: int) -> None:
    """
    Purpose:    Permanently remove a saved strategy. Does not affect any real
                trades — strategies are never tagged onto the trade ledger.
    Args:       db (AsyncSession): The active database session.
                strategy_id (int): Identifier of the strategy to delete.
    Returns:    None.
    Raises:     HTTPException: 404 if the strategy doesn't exist.
    """
    strategy = await get_strategy(db, strategy_id)
    await db.delete(strategy)
    await db.commit()
