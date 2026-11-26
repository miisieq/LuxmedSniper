"""Argument parsing and the exit codes.

`cli.py` had no coverage at all, which is how the empty `-c` case below went unnoticed: it is the
one path where the process neither works nor stops.
"""

import json

import pytest

from luxmedsniper import cli
from luxmedsniper.cli import build_parser, main

# --- exit codes ------------------------------------------------------------------------------


def test_a_missing_configuration_file_exits_with_one(capsys):
    """The Dockerfile's missing-bind-mount guard: without this the process would log
    IsADirectoryError every cycle while still reporting itself as running.

    capsys rather than the `log` fixture: main() calls setup_logging(), whose
    logger.configure() replaces every handler, including one a fixture installed.
    """
    assert main(["-c", "definitely-not-here.yaml"]) == 1
    assert "Not a readable configuration file" in capsys.readouterr().out


def test_a_directory_in_place_of_a_configuration_file_exits_with_one(tmp_path):
    """Docker creates a directory when a bind mount source does not exist."""
    assert main(["-c", str(tmp_path)]) == 1


def test_c_without_a_value_exits_instead_of_looping_forever(capsys):
    """`nargs="*"` yields an empty list, which used to sail past the readable-file loop, load an
    empty config, and leave the scheduler running a cycle that could never work - a KeyError per
    interval and a CRITICAL alert after three."""
    assert main(["-c"]) == 1
    assert "No configuration file given" in capsys.readouterr().out


def test_one_unreadable_file_among_several_is_enough_to_stop(tmp_path):
    good = tmp_path / "good.yaml"
    good.write_text("luxmed: {login: jan, password: pw}\n", encoding="utf-8")

    assert main(["-c", str(good), "missing.yaml"]) == 1


# --- argument defaults -----------------------------------------------------------------------


def test_the_default_config_path_is_the_one_the_readme_promises():
    assert build_parser().parse_args([]).config == ["luxmed_sniper.yaml"]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (None, 1800),
        ("", 1800),  # compose passes an unset variable through as an empty string
        ("900", 900),
    ],
)
def test_the_interval_comes_from_the_environment_with_a_fallback(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("LUXMED_INTERVAL", raising=False)
    else:
        monkeypatch.setenv("LUXMED_INTERVAL", env)

    assert build_parser().parse_args([]).delay == expected


def test_an_explicit_delay_beats_the_environment(monkeypatch):
    monkeypatch.setenv("LUXMED_INTERVAL", "900")

    assert build_parser().parse_args(["-d", "60"]).delay == 60


def test_several_config_files_are_collected_for_merging():
    """Splitting users, doctors and providers across files is a documented workflow."""
    args = build_parser().parse_args(["-c", "a.yaml", "b.yaml"])

    assert args.config == ["a.yaml", "b.yaml"]


def test_the_dump_ids_flags_default_to_off():
    args = build_parser().parse_args([])

    assert args.dump_ids is False
    assert args.dump_ids_doctors is False
    assert args.dump_ids_city is None


# --- dumping the id dictionaries -------------------------------------------------------------


class StubPortal:
    """Stands in for the real client: no network, no credentials."""

    def __init__(self, *args, **kwargs):
        self.logged_in = False

    def login(self):
        self.logged_in = True

    def get_cities(self):
        return [{"id": 45, "name": "Trójmiasto"}]

    def get_services(self):
        return [{"id": 15, "name": "Konsultacje", "isTelemedicine": False, "children": []}]

    def get_facilities_and_doctors(self, city_id, service_variant_id):
        return {
            "facilities": [{"id": 294, "name": "Gdańsk - al. Grunwaldzka 347"}],
            "doctors": [{"id": 1, "academicTitle": "lek.", "firstName": "Jan", "lastName": "Kowalski"}],
        }


@pytest.fixture
def dumping(monkeypatch, tmp_path):
    """Runs dump_current_ids against a stub portal, writing into a throwaway directory."""
    monkeypatch.setattr(cli, "LuxMedPortal", StubPortal)
    monkeypatch.setattr(cli, "IDS_DIR", tmp_path)

    config = tmp_path / "c.yaml"
    config.write_text("luxmed: {login: jan, password: pw}\n", encoding="utf-8")

    def run(city_wildcard=None, *, doctors=False):
        cli.dump_current_ids([str(config)], city_wildcard, doctors)
        return tmp_path

    return run


def test_dump_ids_alone_leaves_an_existing_facilities_dump_untouched(dumping, tmp_path):
    """The reported bug: a plain --dump-ids replaced a 500 kB facilities dump with `{}`.

    That dump costs hundreds of requests against an account LUX MED locks for hammering, so
    destroying it is expensive and not obviously recoverable.
    """
    sentinel = tmp_path / "ids-facilities-doctors.json"
    sentinel.write_text('{"45": "precious"}', encoding="utf-8")

    dumping()

    assert json.loads(sentinel.read_text(encoding="utf-8")) == {"45": "precious"}


def test_cities_and_services_are_still_written_without_doctors(dumping):
    out = dumping()

    assert json.loads((out / "ids-cities.json").read_text())[0]["name"] == "Trójmiasto"
    assert json.loads((out / "ids-services.json").read_text())[0]["id"] == 15


def test_asking_for_doctors_writes_the_facilities_dump(dumping):
    out = dumping(doctors=True)

    dumped = json.loads((out / "ids-facilities-doctors.json").read_text())
    assert dumped["45"]["15"]["doctors"] == [{"id": 1, "name": "lek. Kowalski Jan"}]


def test_a_city_filter_names_the_file_after_it(dumping):
    out = dumping("Trójmiasto", doctors=True)

    assert (out / "ids-facilities-doctors-trojmiasto.json").exists()
    assert not (out / "ids-facilities-doctors.json").exists(), (
        "two cities must not fight over one filename"
    )


def test_a_city_filter_that_matches_nothing_still_writes_no_facilities(dumping):
    out = dumping("Nowhere*", doctors=True)

    dumped = json.loads((out / "ids-facilities-doctors-nowhere.json").read_text())
    assert dumped == {}


@pytest.mark.parametrize(
    ("wildcard", "expected"),
    [
        (None, "ids-facilities-doctors.json"),
        ("Trójmiasto", "ids-facilities-doctors-trojmiasto.json"),
        ("Warszawa*", "ids-facilities-doctors-warszawa.json"),
        ("Łódź", "ids-facilities-doctors-lodz.json"),
        ("Zielona Góra", "ids-facilities-doctors-zielona-gora.json"),
        ("*", "ids-facilities-doctors.json"),  # an empty slug falls back rather than trailing a dash
    ],
)
def test_the_facilities_filename_is_derived_from_the_city_pattern(wildcard, expected):
    assert cli.facilities_filename(wildcard) == expected


def test_the_service_tree_is_flattened_to_three_levels():
    raw = [{
        "id": 1, "name": "Konsultacje", "isTelemedicine": False,
        "children": [{
            "id": 2, "name": "Internista", "isTelemedicine": False,
            "children": [{"id": 3, "name": "Wizyta", "isTelemedicine": True}],
        }],
    }]

    assert cli.flatten_services(raw) == [
        {"id": 1, "name": "Konsultacje", "telemedicine": False},
        {"id": 2, "name": "Internista", "telemedicine": False},
        {"id": 3, "name": "Wizyta", "telemedicine": True},
    ]
