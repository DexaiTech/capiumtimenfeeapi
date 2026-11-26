"""MySQL building blocks that need no database: URLs, TLS, error meanings, UTC datetimes and
the SQL of the named locks."""

import ssl
from datetime import UTC, datetime, timedelta, timezone

import pymysql
import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config import Settings, normalise_db_url
from app.db.errors import database_error, is_overlap
from app.db.locks import lock_key
from app.db.session import SESSION_INIT, engine_options, ssl_context
from app.db.types import UTCDateTime

DIALECT = mysql.dialect()


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("mysql://u:p@db:3306/capium", "mysql+aiomysql://u:p@db:3306/capium?charset=utf8mb4"),
        ("mysql+pymysql://u@db/capium", "mysql+aiomysql://u@db/capium?charset=utf8mb4"),
        (
            "mysql://u:p@db/capium?charset=latin1&connect_timeout=5",
            "mysql+aiomysql://u:p@db/capium?connect_timeout=5&charset=utf8mb4",
        ),
        ("mysql://u:p@ss@db/capium", "mysql+aiomysql://u:p%40ss@db/capium?charset=utf8mb4"),
    ],
)
def test_a_mysql_url_is_normalised_for_the_async_driver(given, expected):
    assert normalise_db_url(given) == expected


@pytest.mark.parametrize(
    "url", ["postgresql://u@db/capium", "sqlite:///x.db", "mysql://u@db/capium?ssl-mode=REQUIRED"]
)
def test_other_databases_and_tls_in_the_url_are_refused(url):
    with pytest.raises(ValueError):
        normalise_db_url(url)


def _settings(**values: str) -> Settings:
    return Settings(
        db_url="mysql://u:p@db/capium",
        jwt_secret_key="k" * 40,
        frontend_url="http://localhost:5173",
        backend_url="http://localhost:8000",
        **values,
    )


def test_tls_follows_the_ssl_mode():
    assert ssl_context(_settings()) is None
    required = ssl_context(_settings(db_ssl_mode="required"))
    assert required is not None and required.verify_mode == ssl.CERT_NONE
    verify_ca = ssl_context(_settings(db_ssl_mode="verify_ca"))
    assert verify_ca is not None
    assert verify_ca.verify_mode == ssl.CERT_REQUIRED and not verify_ca.check_hostname
    identity = ssl_context(_settings(db_ssl_mode="verify_identity"))
    assert identity is not None and identity.check_hostname


def test_an_unknown_ssl_mode_is_refused():
    with pytest.raises(ValidationError):
        _settings(db_ssl_mode="prefer")


def test_every_connection_is_utc_strict_and_read_committed():
    options = engine_options(_settings(db_ssl_mode="verify_identity"))
    assert options["isolation_level"] == "READ COMMITTED"
    assert options["connect_args"]["init_command"] == SESSION_INIT
    assert isinstance(options["connect_args"]["ssl"], ssl.SSLContext)
    assert "time_zone = '+00:00'" in SESSION_INIT and "STRICT_TRANS_TABLES" in SESSION_INIT
    assert "ssl" not in engine_options(_settings())["connect_args"]


def _wrapped(error: Exception, cls: type = IntegrityError) -> Exception:
    return cls("INSERT ...", {}, error)


@pytest.mark.parametrize(
    ("error", "kind", "constraint"),
    [
        (
            pymysql.err.IntegrityError(
                1062, "Duplicate entry 'a@x' for key 'users.uq_users_email_lower'"
            ),
            "unique",
            "uq_users_email_lower",
        ),
        (
            pymysql.err.IntegrityError(
                1452,
                "Cannot add or update a child row: a foreign key constraint fails (`capium`."
                "`jobs`, CONSTRAINT `fk_jobs_client_id_clients` FOREIGN KEY (`client_id`) ...)",
            ),
            "foreign_key",
            "fk_jobs_client_id_clients",
        ),
        (
            pymysql.err.OperationalError(3819, "Check constraint 'ck_jobs_status' is violated."),
            "check",
            "ck_jobs_status",
        ),
        (
            pymysql.err.OperationalError(1644, "ex_rate_rules_scope_period"),
            "overlap",
            "ex_rate_rules_scope_period",
        ),
        (pymysql.err.OperationalError(1213, "Deadlock found"), "deadlock", None),
        (pymysql.err.OperationalError(1205, "Lock wait timeout exceeded"), "lock_timeout", None),
        (pymysql.err.DataError(1406, "Data too long for column 'name'"), "too_long", None),
        (pymysql.err.OperationalError(2003, "Can't connect"), "other", None),
    ],
)
def test_a_mysql_error_is_classified_with_its_constraint(error, kind, constraint):
    found = database_error(_wrapped(error))
    assert found is not None
    assert (found.kind, found.constraint) == (kind, constraint)


def test_only_an_overlap_trigger_is_an_overlap():
    overlap = _wrapped(pymysql.err.OperationalError(1644, "ex_rate_rules_scope_period"))
    assert is_overlap(overlap) and is_overlap(overlap, "ex_rate_rules_scope_period")
    assert not is_overlap(overlap, "ex_staff_hours_period")
    duplicate = _wrapped(pymysql.err.IntegrityError(1062, "Duplicate entry 'x' for key 'k'"))
    assert not is_overlap(duplicate)
    assert database_error(_wrapped(RuntimeError("no number"), OperationalError)) is None


def test_datetimes_are_stored_as_utc_and_read_back_aware():
    column = UTCDateTime()
    india = timezone(timedelta(hours=5, minutes=30))
    stored = column.process_bind_param(datetime(2026, 11, 26, 10, 0, tzinfo=india), DIALECT)
    assert stored == datetime(2026, 11, 26, 4, 30)
    assert stored is not None and stored.tzinfo is None
    # A naive value is already UTC, as it was in a UTC PostgreSQL session.
    assert column.process_bind_param(datetime(2026, 1, 1, 12), DIALECT) == datetime(2026, 1, 1, 12)
    read = column.process_result_value(datetime(2026, 11, 26, 4, 30, 0, 123456), DIALECT)
    assert read == datetime(2026, 11, 26, 4, 30, 0, 123456, tzinfo=UTC)
    assert column.process_bind_param(None, DIALECT) is None
    assert column.process_result_value(None, DIALECT) is None


def test_datetime_columns_keep_microseconds():
    assert str(UTCDateTime().compile(dialect=DIALECT)) == "DATETIME(6)"


def test_lock_keys_fit_the_column_and_are_stable():
    key = lock_key("fixed_fee:" + "x" * 500)
    assert len(key) == 64 and key == lock_key("fixed_fee:" + "x" * 500)
    assert key != lock_key("fixed_fee:y")
