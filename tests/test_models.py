"""Types and the error vocabulary."""

import pytest
import yaml
from conftest import PASSWORD

from luxmedsniper.models import DoctorLocator, Luxmed, LuxmedSniperError, describe_failure


@pytest.mark.parametrize(
    ("status", "expected"),
    [(503, "HTTP 503 Service Unavailable"), (401, "HTTP 401 Unauthorized"), (599, "HTTP 599")],
)
def test_describe_failure_handles_unknown_codes(status, expected):
    assert describe_failure(LuxmedSniperError("boom", status)) == expected


def test_describe_failure_uses_our_own_wording():
    """LuxmedSniperError text is authored here, so it is safe to forward to a phone."""
    assert describe_failure(LuxmedSniperError("Conflict at .luxmed")) == "Conflict at .luxmed"


def test_describe_failure_falls_back_to_the_exception_class():
    """Anything we did not write may quote the config file, so only the type is reported."""
    try:
        yaml.safe_load(f"luxmed:\n  password: {PASSWORD}\n bad indent\n")
    except yaml.YAMLError as parser_error:
        described = describe_failure(parser_error)

    assert PASSWORD not in described
    # Only the class name - which subclass PyYAML raises is its business, not ours.
    assert described.endswith("Error")
    assert "\n" not in described, "a quoted config snippet would span lines"


def test_describe_failure_names_a_bare_exception_by_type():
    assert describe_failure(ValueError("secret in here")) == "ValueError"


# --- DoctorLocator.id parsing ----------------------------------------------------------------


def test_locator_id_is_split_into_its_four_parts():
    locator = DoctorLocator.model_validate({"id": "45*4502*12,13*67013,67014"})

    assert locator.city_id == "45"
    assert locator.service_id == "4502"
    assert locator.clinic_ids == [12, 13]
    assert locator.doctor_ids == [67013, 67014]


def test_minus_one_means_any():
    locator = DoctorLocator.model_validate({"id": "45*4502*-1*-1"})

    assert locator.clinic_ids == []
    assert locator.doctor_ids == []


def test_name_defaults_to_the_raw_id_and_enabled_defaults_to_true():
    locator = DoctorLocator.model_validate({"id": "45*4502*-1*-1"})

    assert locator.name == "45*4502*-1*-1"
    assert locator.enabled is True


@pytest.mark.parametrize("bad_id", ["45*4502*-1", "45*4502*-1*-1*extra", "nonsense", ""])
def test_a_malformed_id_is_rejected_with_the_expected_format(bad_id):
    with pytest.raises(ValueError, match="cityId\\*serviceId\\*facilitiesIds\\*doctorsIds"):
        DoctorLocator.model_validate({"id": bad_id})


# --- Luxmed credentials ----------------------------------------------------------------------


def test_login_alone_is_accepted():
    assert Luxmed(login="jan", password="pw").login == "jan"


def test_email_alone_is_accepted_and_fills_in_login(log):
    """`email` is the deprecated spelling, still honoured."""
    luxmed = Luxmed(email="jan@example.invalid", password="pw")

    assert luxmed.login == "jan@example.invalid"
    assert "deprecated" in log.getvalue()


def test_both_login_and_email_is_rejected():
    with pytest.raises(ValueError, match="exactly one of"):
        Luxmed(login="jan", email="jan@example.invalid", password="pw")


def test_neither_login_nor_email_is_rejected():
    with pytest.raises(ValueError, match="exactly one of"):
        Luxmed(password="pw")
