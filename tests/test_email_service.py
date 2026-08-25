import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.services import email
from config.settings import settings


@pytest.fixture(autouse=True)
def _reset_smtp_settings(monkeypatch):
    # Every test starts from "email disabled" and opts in explicitly, so a
    # leftover smtp_host from another test can never leak in.
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_username", "")
    monkeypatch.setattr(settings, "smtp_password", "")
    monkeypatch.setattr(settings, "smtp_use_tls", True)
    monkeypatch.setattr(settings, "smtp_from_email", "")
    monkeypatch.setattr(settings, "smtp_from_name", "QuantSphere")


def test_email_disabled_by_default_and_send_is_a_noop():
    assert settings.email_enabled is False
    result = asyncio.run(email.send_email("user@example.com", "Subject", "Body"))
    assert result is False


@patch("app.services.email.smtplib.SMTP")
def test_send_email_uses_smtp_when_configured(mock_smtp_cls, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_username", "bot@example.com")
    monkeypatch.setattr(settings, "smtp_password", "secret")
    assert settings.email_enabled is True

    mock_server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_server

    result = asyncio.run(email.send_email("user@example.com", "Hi", "Hello there"))

    assert result is True
    mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=15)
    mock_server.starttls.assert_called_once()
    mock_server.login.assert_called_once_with("bot@example.com", "secret")
    mock_server.send_message.assert_called_once()
    sent_msg = mock_server.send_message.call_args[0][0]
    assert sent_msg["To"] == "user@example.com"
    assert sent_msg["Subject"] == "Hi"


@patch("app.services.email.smtplib.SMTP")
def test_send_email_swallows_smtp_failures(mock_smtp_cls, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    mock_smtp_cls.side_effect = OSError("connection refused")

    result = asyncio.run(email.send_email("user@example.com", "Hi", "Hello"))
    assert result is False


@patch("app.services.email.send_email")
def test_send_welcome_email_mentions_the_account_email(mock_send_email):
    mock_send_email.return_value = True
    asyncio.run(email.send_welcome_email("newuser@example.com"))

    mock_send_email.assert_called_once()
    to_email, subject, body = mock_send_email.call_args[0]
    assert to_email == "newuser@example.com"
    assert "welcome" in subject.lower()
    assert "newuser@example.com" in body


@patch("app.services.email.send_email")
def test_send_password_reset_email_includes_the_link(mock_send_email):
    mock_send_email.return_value = True
    asyncio.run(email.send_password_reset_email("user@example.com", "https://app.example.com/?reset_token=abc123"))

    mock_send_email.assert_called_once()
    to_email, subject, body = mock_send_email.call_args[0]
    assert to_email == "user@example.com"
    assert "reset" in subject.lower()
    assert "https://app.example.com/?reset_token=abc123" in body
