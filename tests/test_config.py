"""Loading, merging and redacting the YAML configuration."""

import pytest

from luxmedsniper.config import build_locators, load_configuration, redacted
from luxmedsniper.models import LuxmedSniperError


def write(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_several_files_are_merged(tmp_path):
    a = write(tmp_path, "a.yaml", "luxmed: {login: jan}\n")
    b = write(tmp_path, "b.yaml", "misc: {notifydb: ./n.db}\n")

    merged = load_configuration([a, b])

    assert merged == {"luxmed": {"login": "jan"}, "misc": {"notifydb": "./n.db"}}


def test_nested_sections_merge_key_by_key(tmp_path):
    a = write(tmp_path, "a.yaml", "luxmed: {login: jan}\n")
    b = write(tmp_path, "b.yaml", "luxmed: {password: pw}\n")

    assert load_configuration([a, b])["luxmed"] == {"login": "jan", "password": "pw"}


def test_the_same_value_in_two_files_is_not_a_conflict(tmp_path):
    a = write(tmp_path, "a.yaml", "luxmed: {login: jan}\n")
    b = write(tmp_path, "b.yaml", "luxmed: {login: jan}\n")

    assert load_configuration([a, b])["luxmed"]["login"] == "jan"


def test_two_files_disagreeing_on_a_value_is_an_error(tmp_path):
    a = write(tmp_path, "a.yaml", "luxmed: {login: jan}\n")
    b = write(tmp_path, "b.yaml", "luxmed: {login: anna}\n")

    with pytest.raises(LuxmedSniperError, match=r"Conflict at .*login"):
        load_configuration([a, b])


# --- redaction -------------------------------------------------------------------------------


def test_every_known_secret_key_is_masked():
    config = {
        "luxmed": {"login": "jan", "password": "pw"},
        "pushover": {"user_key": "uk", "api_token": "at"},
        "pushbullet": {"access_token": "tok"},
        "other": {"token": "t"},
    }

    masked = redacted(config)

    assert masked["luxmed"] == {"login": "jan", "password": "***"}
    assert masked["pushover"] == {"user_key": "***", "api_token": "***"}
    assert masked["pushbullet"]["access_token"] == "***"
    assert masked["other"]["token"] == "***"


def test_an_empty_secret_is_left_alone_so_it_reads_as_unset():
    assert redacted({"pushover": {"api_token": None}})["pushover"]["api_token"] is None


def test_redacting_does_not_mutate_the_original():
    config = {"luxmed": {"password": "pw"}}

    redacted(config)

    assert config["luxmed"]["password"] == "pw"


# --- locators --------------------------------------------------------------------------------


def test_locators_are_built_from_the_luxmedsniper_section():
    config = {
        "luxmedsniper": {
            "doctor_locators": [
                {"id": "45*4502*-1*-1", "name": "First"},
                {"id": "45*4565*-1*-1", "name": "Second", "enabled": False},
            ],
            "facilities_ids": [],
        },
    }

    built = build_locators(config)

    assert [locator.name for locator in built.locators] == ["First", "Second"]
    assert [locator.enabled for locator in built.locators] == [True, False]
    assert built.errors == []


def test_global_facilities_ids_are_appended_to_every_locator():
    """This is the only reason build_locators passes a validation context."""
    config = {
        "luxmedsniper": {
            "doctor_locators": [{"id": "45*4502*7*-1"}],
            "facilities_ids": [11, 12],
        },
    }

    assert build_locators(config).locators[0].clinic_ids == [7, 11, 12]


def test_no_facilities_ids_key_is_not_an_error():
    config = {"luxmedsniper": {"doctor_locators": [{"id": "45*4502*7*-1"}]}}

    assert build_locators(config).locators[0].clinic_ids == [7]


def test_one_unparseable_locator_does_not_cost_the_others_their_searches(log):
    """The regression that prompted this: eager validation of the whole list meant a single
    typo silently stopped every working search."""
    config = {
        "luxmedsniper": {
            "doctor_locators": [
                {"id": "45*4502*-1*-1", "name": "Good"},
                {"id": "BROKEN", "name": "Bad"},
                {"id": "45*4565*-1*-1", "name": "AlsoGood"},
            ],
            "facilities_ids": [],
        },
    }

    built = build_locators(config)

    assert [locator.name for locator in built.locators] == ["Good", "AlsoGood"]
    assert len(built.errors) == 1
    assert "BROKEN" in built.errors[0]
    assert "Skipping unparseable locator" in log.getvalue()


def test_the_reported_error_never_quotes_anything_but_the_locator():
    """The error travels into `last_error` and from there into a Pushover message."""
    config = {
        "luxmedsniper": {
            "doctor_locators": [{"id": "BROKEN", "name": "Bad"}],
            "facilities_ids": [],
        },
        "luxmed": {"password": "PASSWORD_THAT_MUST_NOT_LEAK"},
    }

    [error] = build_locators(config).errors

    assert "PASSWORD_THAT_MUST_NOT_LEAK" not in error


def test_every_locator_broken_is_reported_as_failures_not_as_an_empty_config():
    config = {
        "luxmedsniper": {"doctor_locators": [{"id": "A"}, {"id": "B"}], "facilities_ids": []},
    }

    built = build_locators(config)

    assert built.locators == []
    assert len(built.errors) == 2, "run_checks counts these, so an all-broken config still alerts"
