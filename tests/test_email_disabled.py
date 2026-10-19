"""Without SMTP_HOST outgoing email is off: the app starts and email jobs succeed silently."""

from app.core.config import Settings, get_settings
from app.services.email import EmailService


def _settings_without_smtp(**overrides: object) -> Settings:
    base = get_settings().model_dump()
    base.update(smtp_host="", **overrides)
    return Settings.model_validate(base)


def test_settings_load_without_any_smtp_configuration() -> None:
    settings = _settings_without_smtp()
    assert settings.smtp_host == ""
    # Neither SMTP setting is required any more (the test environment sets the address).
    assert Settings.model_fields["smtp_host"].default == ""
    assert Settings.model_fields["smtp_from_address"].default == "no-reply@localhost"


def test_production_needs_smtp_credentials_only_when_a_host_is_set() -> None:
    production = {
        "app_env": "production",
        "app_debug": False,
        "jwt_secret_key": "x" * 48,
        "auth_rate_limit_factor": 1,
    }
    assert _settings_without_smtp(**production).is_production
    try:
        Settings.model_validate(
            {**get_settings().model_dump(), **production, "smtp_host": "smtp.example.com"}
        )
    except ValueError as exc:
        assert "SMTP_USER and SMTP_PASSWORD" in str(exc)
    else:
        raise AssertionError("an SMTP host without credentials must be refused in production")


async def test_sending_is_skipped_when_email_is_off(mailbox) -> None:
    service = EmailService(_settings_without_smtp())
    await service.send_template("welcome", to="ada@example.com", name="Ada")
    assert mailbox.messages == []
