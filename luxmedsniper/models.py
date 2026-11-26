"""Shared types and the error vocabulary.

This module is the root of the import graph: it depends on nothing else in the project, which is
what keeps the rest of the modules free of cycles. Keep it that way - nothing here may import
config, portal, notifications or runner.
"""

import datetime
import typing
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field, model_validator


class LuxmedSniperError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code  # lets the alert name the HTTP code that caused the outage


def describe_failure(error: BaseException) -> str:
    """Names a failure the way an alert should read: the HTTP code and its label when there was
    a response, the exception class when the request never got that far."""
    status = getattr(error, "status_code", None)
    if status is None:
        # Our own wording is safe to forward; a parser error is not, because it quotes the
        # offending line of the config - which may well be the one holding the password.
        return str(error) if isinstance(error, LuxmedSniperError) else type(error).__name__
    try:
        return f"HTTP {status} {HTTPStatus(status).phrase}"
    except ValueError:  # the portal is free to invent codes the stdlib does not know
        return f"HTTP {status}"


class DoctorLocator(BaseModel):
    id: str
    name: str | None = None
    enabled: bool = True

    city_id: str
    service_id: str
    clinic_ids: list[int] = Field(default_factory=list)
    doctor_ids: list[int] = Field(default_factory=list)

    referral_id: int | None = None  # the patient's own referral; unlocks slots reserved for its holders
    referral_type_id: int | None = None  # the portal sent 3 for a follow-up visit (wizyta kontrolna)

    @model_validator(mode="after")
    def _referral_fields_come_together(self) -> "DoctorLocator":
        if (self.referral_id is None) != (self.referral_type_id is None):
            raise ValueError("referral_id and referral_type_id must be set together")
        return self

    @model_validator(mode="before")
    @classmethod
    def _parse_from_id_and_config(cls, data: Any, info):
        if not isinstance(data, dict):
            return data

        raw_id = str(data.get("id", "")).strip()
        try:
            city_id, service_id, clinic_ids_str, doctor_ids_str = raw_id.split("*")
        except ValueError as e:
            raise ValueError(
                "doctor_locator.id must have format: cityId*serviceId*facilitiesIds*doctorsIds (example: 1*7409*-1*-1)",
            ) from e

        def _parse_ids(s: str) -> list[int]:
            return [i for i in map(int, s.split(",")) if i != -1]

        clinic_ids = _parse_ids(clinic_ids_str)
        doctor_ids = _parse_ids(doctor_ids_str)

        luxmedsniper_config = (info.context or {}).get("luxmedsniper_config", {}) if info else {}
        clinic_ids += list(luxmedsniper_config.get("facilities_ids", []) or [])

        return {
            **data,
            "city_id": city_id,
            "service_id": service_id,
            "clinic_ids": clinic_ids,
            "doctor_ids": doctor_ids,
            "name": data.get("name") or raw_id,
            "enabled": data.get("enabled", True),
        }


class CheckResult(typing.NamedTuple):
    """Outcome of one cycle. `enabled` separates "nothing to do" from "everything failed"."""

    enabled: int
    searched: int
    last_error: str = ""  # most recent failure, so the alert can name what is actually wrong


class LocatorSet(typing.NamedTuple):
    """The searches that parsed, and a description of each one that did not.

    Both halves are needed. Dropping the failures silently would let a config whose every locator
    is malformed look like a config whose every locator is switched off - nothing searched, but
    nothing wrong either, so no alert.
    """

    locators: list["DoctorLocator"]
    errors: list[str]


@dataclass
class Appointment:
    AppointmentDate: datetime.datetime
    clinic_id: int | None
    ClinicPublicName: str
    DoctorName: str
    service_id: int | None


class Luxmed(BaseModel):
    password: str
    login: str | None = None
    email: str | None = None

    model_config = {"validate_assignment": True}

    @model_validator(mode="before")
    @classmethod
    def _validate_login_or_email(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        login = data.get("login")
        email = data.get("email")

        # exactly one of login/email must be provided
        if bool(login) == bool(email):
            raise ValueError("Provide exactly one of: luxmed.login or luxmed.email")

        # email is deprecated but still supported
        if email and not login:
            logger.warning("Config field luxmed.email is deprecated; use luxmed.login instead.")
            data["login"] = email

        return data
