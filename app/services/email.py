import asyncio
import logging
import smtplib
from email.message import EmailMessage

from config.settings import settings

logger = logging.getLogger("quantsphere.email")


def _send_sync(to_email: str, subject: str, body_text: str) -> None:
    """
    Purpose:    The actual blocking SMTP send — smtplib has no async API, so
                this always runs off the event loop via asyncio.to_thread.
    Args:       to_email (str): Recipient address.
                subject (str): Email subject line.
                body_text (str): Plain-text email body.
    Returns:    None.
    Raises:     Exception: Any smtplib/socket error — caller always catches
                    this; a failed email must never break the request that
                    triggered it (signup, login, password reset).
    """
    msg = EmailMessage()
    from_email = settings.smtp_from_email or settings.smtp_username
    msg["From"] = f"{settings.smtp_from_name} <{from_email}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body_text)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
        if settings.smtp_use_tls:
            server.starttls()
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(msg)


async def send_email(to_email: str, subject: str, body_text: str) -> bool:
    """
    Purpose:    Best-effort account email delivery — the single entry point
                every account-email helper below goes through. Never raises:
                a broken mail server must never break signup, login, or
                password reset, which all work fine without it.
    Args:       to_email (str): Recipient address.
                subject (str): Email subject line.
                body_text (str): Plain-text email body.
    Returns:    bool: True if the send call completed without error, False
                    if email is disabled (no SMTP host configured) or the
                    send failed for any reason.
    Raises:     None.
    """
    if not settings.email_enabled:
        logger.info("Email disabled (no SMTP host configured) — skipping %r to %s", subject, to_email)
        return False
    try:
        await asyncio.to_thread(_send_sync, to_email, subject, body_text)
        return True
    except Exception:
        logger.exception("Failed to send email %r to %s", subject, to_email)
        return False


async def send_welcome_email(to_email: str) -> bool:
    """
    Purpose:    Confirmation email sent right after a successful signup.
    Args:       to_email (str): The new account's email address.
    Returns:    bool: True if sent (or would have been, minus real failures).
    Raises:     None.
    """
    body = (
        "Welcome to QuantSphere!\n\n"
        f"Your account ({to_email}) has just been created — you're all set to start trading.\n\n"
        "QuantSphere is an AI-powered trading journal that turns your own trade history into "
        "honest, data-backed feedback instead of generic advice. A few things you can do:\n\n"
        "  - Log trades (manually, at live market prices, or synced from MetaTrader 5) and get "
        "an instant rules-based grade on every one.\n"
        "  - See your Trading DNA: your real strongest setups, best trading windows, and "
        "recurring mistakes, computed from your own closed trades.\n"
        "  - Ask the local AI Coach for feedback, and use Decision Training to sharpen your "
        "instincts against real historical price data.\n"
        "  - Get Smart Alerts if you're about to repeat a known mistake (revenge trading, "
        "overtrading, oversizing) in real time.\n\n"
        "Every number QuantSphere shows you comes from your real trades — if there isn't enough "
        "data yet for something, it says so instead of making it up.\n\n"
        "If you didn't create this account, you can ignore this email."
    )
    return await send_email(to_email, "Welcome to QuantSphere", body)


async def send_password_reset_email(to_email: str, reset_link: str) -> bool:
    """
    Purpose:    Password-reset email carrying the one-time reset link.
    Args:       to_email (str): The account's email address.
                reset_link (str): Full URL (including the raw token) the
                    user clicks to set a new password.
    Returns:    bool: True if sent.
    Raises:     None.
    """
    body = (
        "Someone (hopefully you) requested a password reset for your "
        f"QuantSphere account ({to_email}).\n\n"
        f"Reset your password here:\n{reset_link}\n\n"
        "This link expires in 1 hour and can only be used once.\n\n"
        "If you didn't request this, you can safely ignore this email — "
        "your password won't change."
    )
    return await send_email(to_email, "Reset your QuantSphere password", body)
