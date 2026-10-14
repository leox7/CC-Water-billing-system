import logging

import pytest

from app.core.errors import (
    AppError,
    AuthenticationRequiredError,
    BusinessRuleError,
    ConflictError,
    InternalError,
    NotFoundError,
    PermissionDeniedError,
    TooManyAttemptsError,
    unexpected_error,
)


@pytest.mark.parametrize(
    "error_class,status_code",
    [
        (BusinessRuleError, 422),
        (ConflictError, 409),
        (NotFoundError, 404),
        (PermissionDeniedError, 403),
        (AuthenticationRequiredError, 401),
        (TooManyAttemptsError, 429),
        (InternalError, 500),
    ],
)
def test_each_exception_carries_its_status_code(error_class, status_code):
    error = error_class()

    assert isinstance(error, AppError)
    assert error.status_code == status_code


def test_default_messages_are_plain_and_specific_to_the_error():
    assert NotFoundError().message == "We could not find that."
    assert PermissionDeniedError().message == "You do not have access to this page."
    assert InternalError().message == "Something went wrong on our side. Please try again."


@pytest.mark.parametrize(
    "error_class",
    [
        BusinessRuleError,
        ConflictError,
        NotFoundError,
        PermissionDeniedError,
        AuthenticationRequiredError,
        TooManyAttemptsError,
        InternalError,
    ],
)
def test_every_exception_has_a_non_empty_message(error_class):
    assert error_class().message.strip() != ""


def test_a_given_message_and_field_are_kept():
    error = BusinessRuleError(
        "This is lower than last month's reading (200). Please check the number.",
        field="current_reading",
    )

    assert error.message == "This is lower than last month's reading (200). Please check the number."
    assert error.field == "current_reading"
    assert str(error) == error.message


def test_field_is_empty_when_not_given():
    assert ConflictError("A reading for Mary already exists for August.").field is None


def test_unexpected_error_returns_a_safe_500():
    cause = RuntimeError("SELECT * FROM users failed at C:\\secret\\path password=hunter2")

    error = unexpected_error(cause, "record payment")

    assert isinstance(error, InternalError)
    assert error.status_code == 500
    assert error.message == "Something went wrong on our side. Please try again."
    assert "SELECT" not in error.message
    assert "hunter2" not in error.message


def test_unexpected_error_logs_the_cause_with_its_stack_trace(caplog):
    cause = RuntimeError("database went away")

    with caplog.at_level(logging.ERROR, logger="app.errors"):
        unexpected_error(cause, "record payment")

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert "record payment" in record.getMessage()
    assert record.exc_info[1] is cause


def test_the_cause_is_kept_when_raised_with_from():
    cause = ValueError("original problem")

    with pytest.raises(InternalError) as raised:
        try:
            raise cause
        except Exception as exc:
            raise unexpected_error(exc, "save reading") from exc

    assert raised.value.__cause__ is cause
