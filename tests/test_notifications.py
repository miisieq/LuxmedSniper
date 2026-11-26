"""Message formatting and provider dispatch."""

import dataclasses
import datetime

import pytest

from luxmedsniper.models import Appointment, DoctorLocator
from luxmedsniper.notifications import build_notifiers, dispatch, format_message


def locator(spec="45*4502*-1*67013", name="Internist"):
    return DoctorLocator.model_validate({"id": spec, "name": name})


def appointment():
    return Appointment(
        datetime.datetime(2026, 10, 9, 14, 30),
        clinic_id=11,
        ClinicPublicName="ul. Testowa 1",
        DoctorName="lek. Jan Kowalski",
        service_id=4502,
    )


# --- formatting ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [(5, "pon"), (6, "wt"), (7, "śr"), (8, "czw"), (9, "pt"), (10, "sob"), (11, "niedz")],
)
def test_the_weekday_is_polish_whatever_the_locale(day, expected):
    """%a would say "Fri" in the container, which has no Polish locale installed."""
    slot = dataclasses.replace(appointment(), AppointmentDate=datetime.datetime(2026, 10, day, 14, 30))

    assert format_message("{weekday}", locator(), slot) == expected


def test_appointment_fields_are_available():
    rendered = format_message("{AppointmentDate} {ClinicPublicName} {DoctorName}", locator(), appointment())

    assert rendered == "2026-10-09 14:30:00 ul. Testowa 1 lek. Jan Kowalski"


def test_the_search_name_is_available_to_every_provider():
    """It used to work only in console; everything else raised KeyError on it."""
    assert format_message("{name}", locator(name="Okulista"), appointment()) == "Okulista"


def test_the_date_takes_a_format_spec():
    rendered = format_message("{AppointmentDate:%Y-%m-%d %H:%M}", locator(), appointment())

    assert rendered == "2026-10-09 14:30"


def test_the_appointment_wins_the_service_id_collision():
    """Both the search and the slot carry a service_id; the template describes the slot."""
    multi = locator(spec="45*4502,4503*-1*-1")

    assert multi.service_id == "4502,4503"
    assert format_message("{service_id}", multi, appointment()) == "4502"


def test_locator_only_fields_still_come_from_the_locator():
    assert format_message("{city_id}", locator(), appointment()) == "45"


# --- dispatch --------------------------------------------------------------------------------


class Recorder:
    name = "recorder"

    def __init__(self):
        self.sent = []

    def send(self, loc, appt):
        self.sent.append((loc.name, appt.DoctorName))


class Exploding:
    name = "exploding"

    def send(self, loc, appt):
        msg = "provider is down"
        raise RuntimeError(msg)


class AsyncRecorder:
    name = "async"

    def __init__(self):
        self.sent = []

    async def send(self, loc, appt):
        self.sent.append(appt.DoctorName)


def test_every_notifier_is_called():
    first, second = Recorder(), Recorder()

    dispatch([first, second], locator(), appointment())

    assert first.sent and second.sent


def test_a_failing_notifier_does_not_stop_the_others(log):
    survivor = Recorder()

    dispatch([Exploding(), survivor], locator(), appointment())

    assert survivor.sent, "the second provider must still run"
    captured = log.getvalue()
    assert "provider is down" in captured
    assert "exploding" in captured, "the log must name which channel went quiet"


def test_a_coroutine_send_is_awaited():
    recorder = AsyncRecorder()

    dispatch([recorder], locator(), appointment())

    assert recorder.sent == ["lek. Jan Kowalski"]


# --- the factory -----------------------------------------------------------------------------


def test_only_the_selected_providers_are_built():
    config = {
        "luxmedsniper": {"notification_provider": ["console"]},
        "console": {"message_template": "x"},
        "ntfy": {"topic": "t", "message_template": "x"},
    }

    [built] = build_notifiers(config)

    assert built.name == "console"


def test_several_providers_can_run_at_once():
    config = {
        "luxmedsniper": {"notification_provider": ["console", "ntfy"]},
        "console": {"message_template": "x"},
        "ntfy": {"topic": "t", "message_template": "x"},
    }

    assert [n.name for n in build_notifiers(config)] == ["console", "ntfy"]


def test_an_unknown_provider_name_is_skipped_but_said_out_loud(log):
    """Silently dropping a typo leaves a cycle that looks healthy and notifies nobody."""
    config = {
        "luxmedsniper": {"notification_provider": ["console", "pushver"]},
        "console": {"message_template": "x"},
    }

    assert [n.name for n in build_notifiers(config)] == ["console"]
    captured = log.getvalue()
    assert "pushver" in captured
    assert "WARNING" in captured


def test_console_prints_the_rendered_template(capsys):
    config = {
        "luxmedsniper": {"notification_provider": ["console"]},
        "console": {"message_template": "{name}: {DoctorName}"},
    }

    dispatch(build_notifiers(config), locator(), appointment())

    assert capsys.readouterr().out.strip() == "Internist: lek. Jan Kowalski"


def test_pushover_raises_when_the_api_rejects_the_push(monkeypatch, log):
    import requests

    monkeypatch.setattr(
        requests, "post",
        lambda url, **kw: type("R", (), {"status_code": 400, "text": "bad token"})(),
    )
    config = {
        "luxmedsniper": {"notification_provider": ["pushover"]},
        "pushover": {"user_key": "u", "api_token": "a", "title": "t", "message_template": "x"},
    }

    dispatch(build_notifiers(config), locator(), appointment())

    assert "Pushover error: bad token" in log.getvalue()


def test_pushover_cuts_a_title_the_api_would_refuse(monkeypatch):
    """A long search name must not cost the notification: the slot is already recorded as
    announced by the time the push goes out, so a rejected push is never retried."""
    import requests

    sent = {}
    monkeypatch.setattr(
        requests, "post",
        lambda url, **kw: sent.update(kw["data"]) or type("R", (), {"status_code": 200, "text": "{}"})(),
    )
    config = {
        "luxmedsniper": {"notification_provider": ["pushover"]},
        "pushover": {"user_key": "u", "api_token": "a", "title": "{name}" * 100, "message_template": "x"},
    }

    dispatch(build_notifiers(config), locator(), appointment())

    assert sent["title"] == ("Internist" * 100)[:250]


def test_pushover_sends_the_rendered_template(monkeypatch):
    import requests

    sent = {}

    def post(url, **kwargs):
        sent.update(kwargs["data"])
        return type("R", (), {"status_code": 200, "text": "{}"})()

    monkeypatch.setattr(requests, "post", post)
    config = {
        "luxmedsniper": {"notification_provider": ["pushover"]},
        "pushover": {
            "user_key": "u", "api_token": "a", "title": "New visit: {name}",
            "message_template": "{name} - {DoctorName}",
        },
    }

    dispatch(build_notifiers(config), locator(), appointment())

    assert sent["message"] == "Internist - lek. Jan Kowalski"
    assert sent["title"] == "New visit: Internist"
    assert "priority" not in sent, "only the alert sink sets a priority"


# --- config is read when the provider is built, not when it sends ----------------------------
#
# This is the regression that prompted these tests: read at send time, a missing key lands inside
# dispatch's `except`, so the cycle reports itself healthy, the slot is already recorded as
# announced, and the notification is lost for good.


@pytest.mark.parametrize(
    ("section", "settings", "missing"),
    [
        ("pushover", {"user_key": "u", "title": "t", "message_template": "x"}, "api_token"),
        ("pushover", {"api_token": "a", "title": "t", "message_template": "x"}, "user_key"),
        ("pushover", {"api_token": "a", "user_key": "u", "message_template": "x"}, "title"),
        ("pushover", {"api_token": "a", "user_key": "u", "title": "t"}, "message_template"),
        ("ntfy", {"message_template": "x"}, "topic"),
        ("console", {}, "message_template"),
        ("console_async", {}, "message_template"),
        ("sound", {}, "audio"),
    ],
)
def test_a_missing_key_fails_when_the_notifier_is_built(section, settings, missing):
    config = {"luxmedsniper": {"notification_provider": [section]}, section: settings}

    with pytest.raises(KeyError, match=missing):
        build_notifiers(config)


def test_a_broken_pushover_section_never_reaches_the_dedupe_store(tmp_path):
    """End to end on the actual failure: the appointment must not be recorded as announced."""
    from luxmedsniper.state import NotificationStore

    config = {
        "luxmedsniper": {"notification_provider": ["pushover"]},
        "pushover": {"title": "t", "message_template": "x"},  # no credentials
    }
    store = NotificationStore(tmp_path / "n.db")

    with pytest.raises(KeyError):
        build_notifiers(config)

    assert store.is_known(appointment()) is False
    assert not (tmp_path / "n.db").exists()


def test_console_async_is_reachable_through_the_factory(capsys):
    """The coroutine path is otherwise only tested with a hand-rolled class, so a wrong entry in
    the NOTIFIERS map would go unnoticed."""
    config = {
        "luxmedsniper": {"notification_provider": ["console_async"]},
        "console_async": {"message_template": "{name}: {DoctorName}"},
    }

    dispatch(build_notifiers(config), locator(), appointment())

    assert capsys.readouterr().out.strip() == "Internist: lek. Jan Kowalski"


def test_ntfy_posts_the_rendered_template_to_its_topic(monkeypatch):
    import requests

    sent = {}

    def post(url, **kwargs):
        sent["url"] = url
        sent.update(kwargs)
        return type("R", (), {"status_code": 200, "text": "{}"})()

    monkeypatch.setattr(requests, "post", post)
    config = {
        "luxmedsniper": {"notification_provider": ["ntfy"]},
        "ntfy": {"topic": "secret-topic", "message_template": "{name} - {DoctorName}"},
    }

    dispatch(build_notifiers(config), locator(), appointment())

    assert sent["url"] == "https://ntfy.sh/secret-topic"
    assert sent["data"] == "Internist - lek. Jan Kowalski"
    assert sent["headers"]["Title"] == "Nowa wizyta"
