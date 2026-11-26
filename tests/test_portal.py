"""Parsing search results, and the session guard."""

import datetime
import json
import time

import pytest
from conftest import MAINTENANCE_PAGE, OUTAGE_PAGE, FakeResponse

from luxmedsniper.models import DoctorLocator, LuxmedSniperError, PortalMaintenance
from luxmedsniper.portal import LuxMedPortal, parse_appointments


def payload(*terms):
    return {"termsForService": {"termsForDays": [{"terms": list(terms)}]}}


def term(doctor_id=67013, clinic_group_id=1, clinic_id=1):
    return {
        "dateTimeFrom": "2026-10-09T14:30:00",
        "clinicId": clinic_id,
        "clinicGroupId": clinic_group_id,
        "clinic": "ul. Testowa 1",
        "serviceId": 4502,
        "doctor": {
            "id": doctor_id,
            "academicTitle": "lek.",
            "firstName": "Jan",
            "lastName": "Kowalski",
        },
    }


def locator(spec="45*4502*-1*-1", **extra):
    return DoctorLocator.model_validate({"id": spec, **extra})


# --- parsing ---------------------------------------------------------------------------------


def test_a_term_becomes_an_appointment():
    [appointment] = parse_appointments(payload(term()), locator())

    assert appointment.DoctorName == "lek. Jan Kowalski"
    assert appointment.ClinicPublicName == "ul. Testowa 1"
    assert appointment.service_id == 4502
    assert appointment.AppointmentDate.tzinfo is not None, "times are Europe/Warsaw, not naive"


def test_an_empty_schedule_yields_nothing():
    assert parse_appointments(payload(), locator()) == []


def test_a_locator_without_filters_keeps_every_term():
    found = parse_appointments(payload(term(doctor_id=1), term(doctor_id=2)), locator())

    assert len(found) == 2


def test_terms_from_other_doctors_are_dropped():
    found = parse_appointments(
        payload(term(doctor_id=67013), term(doctor_id=99999)),
        locator("45*4502*-1*67013"),
    )

    assert [a.DoctorName for a in found] == ["lek. Jan Kowalski"]


def test_terms_from_other_clinics_are_dropped():
    found = parse_appointments(
        payload(term(clinic_group_id=7), term(clinic_group_id=8)),
        locator("45*4502*7*-1"),
    )

    assert len(found) == 1


def test_both_filters_must_match():
    found = parse_appointments(
        payload(term(doctor_id=67013, clinic_group_id=8)),
        locator("45*4502*7*67013"),
    )

    assert found == [], "the right doctor at the wrong clinic is not a match"


# --- validating what the portal sent ---------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"errors": ["Invalid referral"]},
        {"termsForService": None},
        {"termsForService": {}},
        {"termsForService": {"termsForDays": None}},
        {"termsForService": {"termsForDays": [{}]}},
        {"termsForService": {"termsForDays": [{"terms": None}]}},
        [],
    ],
)
def test_a_reply_without_the_envelope_is_a_failure_not_an_empty_schedule(body):
    with pytest.raises(LuxmedSniperError, match="unexpected shape"):
        parse_appointments(body, locator())


def test_one_malformed_slot_does_not_cost_the_others(log):
    broken = {**term(doctor_id=1), "doctor": None}

    found = parse_appointments(payload(term(doctor_id=2), broken, term(doctor_id=3)), locator())

    assert len(found) == 2
    assert "ERROR|Skipping a malformed slot" in log.getvalue()


@pytest.mark.parametrize(
    "damage",
    [{"doctor": None}, {"dateTimeFrom": "soon"}, {"clinicGroupId": None}, {"doctor": {"firstName": "Jan"}}],
)
def test_a_reply_whose_every_slot_is_malformed_is_a_failure(damage, log):
    """That is the portal changing its format, and reading it as "nothing free" would hide it."""
    with pytest.raises(LuxmedSniperError, match="unexpected shape"):
        parse_appointments(payload({**term(), **damage}, {**term(), **damage}), locator())


def test_the_failure_names_the_field_and_never_the_value(log):
    secretive = {**term(), "dateTimeFrom": "VALUE_THAT_MUST_NOT_LEAK"}

    with pytest.raises(LuxmedSniperError) as raised:
        parse_appointments(payload(secretive), locator())

    assert "dateTimeFrom" in str(raised.value)
    assert "VALUE_THAT_MUST_NOT_LEAK" not in str(raised.value) + log.getvalue()


def test_a_slot_missing_only_decoration_is_still_announced():
    """The names and the address make the message nicer; the slot is real without them."""
    bare = {"dateTimeFrom": "2026-10-09T14:30:00", "clinicGroupId": 1, "doctor": {"id": 67013}}

    [appointment] = parse_appointments(payload(bare), locator())

    assert appointment.DoctorName == ""
    assert appointment.ClinicPublicName == ""


def test_a_doctor_without_a_title_is_not_called_none():
    untitled = {**term(), "doctor": {**term()["doctor"], "academicTitle": None}}

    [appointment] = parse_appointments(payload(untitled), locator())

    assert appointment.DoctorName == "Jan Kowalski"


def test_an_offset_sent_by_the_portal_is_honoured():
    zulu = {**term(), "dateTimeFrom": "2026-10-09T12:30:00Z"}

    [appointment] = parse_appointments(payload(zulu), locator())

    assert appointment.AppointmentDate.isoformat() == "2026-10-09T14:30:00+02:00"


def test_a_time_without_an_offset_is_polish_wall_clock_time(zone_on_another_day):
    """Wherever the machine happens to be: the clinic's clock is the Polish one."""
    [appointment] = parse_appointments(payload(term()), locator())

    assert appointment.AppointmentDate.isoformat() == "2026-10-09T14:30:00+02:00"


# --- session handling ------------------------------------------------------------------------


class StubSession:
    """Stands in for requests.Session so the client can be built without any patching."""

    def __init__(self, response):
        self.headers = {}
        self.cookies = {}
        self._response = response
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        return self._response


def portal_with(response):
    session = StubSession(response)
    return LuxMedPortal("jan", "pw", session=session), session


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_session_raises_with_its_status(status):
    client, _ = portal_with(FakeResponse("GET", "u", status, "Unauthorized", "text/plain"))

    with pytest.raises(LuxmedSniperError, match=f"Session rejected by the portal \\({status}\\)"):
        client._get("https://example.invalid")


def test_a_login_page_answered_as_200_is_also_a_rejected_session():
    """Without X-Requested-With the portal answers 200 with HTML; the non-JSON check catches it."""
    client, _ = portal_with(FakeResponse("GET", "u", 200, "<html>login</html>", "text/html"))

    with pytest.raises(LuxmedSniperError, match="Session rejected"):
        client._get("https://example.invalid")


@pytest.mark.parametrize("status", [400, 500, 503])
def test_an_error_status_raises_even_when_the_body_is_json(status):
    """Otherwise the body goes on to the parser and the alert names a missing key, not the
    status that explains it."""
    client, _ = portal_with(FakeResponse("GET", "u", status, json.dumps({"message": "nope"})))

    with pytest.raises(LuxmedSniperError, match="answered with an error") as raised:
        client._get("https://example.invalid")

    assert raised.value.status_code == status


def test_an_error_page_is_not_mistaken_for_a_rejected_session():
    client, _ = portal_with(FakeResponse("GET", "u", 500, "<html>oops</html>", "text/html"))

    with pytest.raises(LuxmedSniperError, match="answered with an error \\(500\\)"):
        client._get("https://example.invalid")


@pytest.mark.parametrize("call", ["login", "get"])
def test_the_maintenance_page_is_recognised_on_any_endpoint(call):
    """During planned work login and the JSON API alike answer 503 with the same static page."""
    client, _ = portal_with(FakeResponse("POST", "u", 503, MAINTENANCE_PAGE, "text/html"))

    with pytest.raises(PortalMaintenance) as raised:
        client.login() if call == "login" else client._get("https://example.invalid")

    assert raised.value.status_code == 503


def test_a_503_without_the_maintenance_page_is_an_ordinary_failure():
    client, _ = portal_with(FakeResponse("GET", "u", 503, OUTAGE_PAGE, "text/html"))

    with pytest.raises(LuxmedSniperError) as raised:
        client._get("https://example.invalid")

    assert not isinstance(raised.value, PortalMaintenance), "it must still escalate"


def test_the_marker_in_a_successful_reply_is_not_maintenance():
    body = json.dumps({"note": "Przerwa techniczna"})
    client, _ = portal_with(FakeResponse("GET", "u", 200, body))

    assert client._get("https://example.invalid").json() == {"note": "Przerwa techniczna"}


def test_a_json_200_passes_through():
    client, _ = portal_with(FakeResponse("GET", "u", 200, json.dumps({"ok": True})))

    assert client._get("https://example.invalid").json() == {"ok": True}


def test_login_keeps_an_injected_session_so_a_stub_survives_it():
    response = FakeResponse("POST", LuxMedPortal.LUXMED_LOGIN_URL, 200, json.dumps({"token": "t"}))
    client, session = portal_with(response)

    client.login()

    assert client.session is session, "a handed-in session must not be thrown away"
    assert session.requests == [("POST", LuxMedPortal.LUXMED_LOGIN_URL)]


def test_login_sends_the_browser_headers():
    response = FakeResponse("POST", LuxMedPortal.LUXMED_LOGIN_URL, 200, json.dumps({"token": "t"}))
    client, session = portal_with(response)

    client.login()

    assert session.headers["X-Requested-With"] == "XMLHttpRequest"


def test_a_failed_login_raises_carrying_the_status():
    response = FakeResponse("POST", LuxMedPortal.LUXMED_LOGIN_URL, 503, "maintenance", "text/html")
    client, _ = portal_with(response)

    with pytest.raises(LuxmedSniperError) as raised:
        client.login()

    assert raised.value.status_code == 503


# --- search_appointments ---------------------------------------------------------------------


class RecordingSession(StubSession):
    """Keeps the query parameters too, so the server-side filtering can be asserted."""

    def __init__(self, response):
        super().__init__(response)
        self.params = []

    def request(self, method, url, **kwargs):
        self.params.append(kwargs.get("params"))
        return super().request(method, url, **kwargs)


def searching_portal(payload_body):
    session = RecordingSession(FakeResponse("GET", "u", 200, payload_body))
    return LuxMedPortal("jan", "pw", session=session), session


def test_filter_ids_are_sent_to_the_portal():
    """Without these the portal returns every clinic and doctor in the city, and only the
    client-side re-filter in parse_appointments would narrow it down."""
    client, session = searching_portal(json.dumps(payload()))

    client.search_appointments(locator("45*4502*7,8*67013"), lookup_days=14)

    [params] = session.params
    assert params["facilitiesIds"] == [7, 8]
    assert params["doctorsIds"] == [67013]
    assert params["searchPlace.id"] == "45"
    assert params["searchDatePreset"] == 14


def test_no_filter_ids_are_sent_when_the_locator_has_none():
    client, session = searching_portal(json.dumps(payload()))

    client.search_appointments(locator("45*4502*-1*-1"), lookup_days=14)

    [params] = session.params
    assert "facilitiesIds" not in params
    assert "doctorsIds" not in params


def test_a_referral_is_sent_to_the_portal():
    """Slots reserved for referral holders are invisible to a search that does not name one."""
    client, session = searching_portal(json.dumps(payload()))

    client.search_appointments(locator(referral_id=123456789, referral_type_id=3), lookup_days=14)

    [params] = session.params
    assert params["referralId"] == 123456789
    assert params["referralTypeId"] == 3


def test_no_referral_is_sent_when_the_locator_has_none():
    client, session = searching_portal(json.dumps(payload()))

    client.search_appointments(locator(), lookup_days=14)

    [params] = session.params
    assert "referralId" not in params
    assert "referralTypeId" not in params


@pytest.fixture
def zone_on_another_day(monkeypatch):
    """Moves the system zone to wherever the calendar date differs from the UTC one right now,
    and hands back the offset, so a search that still counted in UTC cannot pass by luck."""
    ahead = datetime.datetime.now(tz=datetime.UTC).hour >= 12
    monkeypatch.setenv("TZ", "Pacific/Kiritimati" if ahead else "Etc/GMT+12")  # UTC+14 / UTC-12
    time.tzset()
    yield datetime.timedelta(hours=14 if ahead else -12)
    monkeypatch.undo()
    time.tzset()


def test_the_search_window_follows_the_system_clock_not_utc(zone_on_another_day):
    client, session = searching_portal(json.dumps(payload()))

    client.search_appointments(locator(), lookup_days=14)

    today = (datetime.datetime.now(tz=datetime.UTC) + zone_on_another_day).date()
    assert today != datetime.datetime.now(tz=datetime.UTC).date(), "the fixture must pick another day"
    [params] = session.params
    assert params["searchDateFrom"] == today.isoformat()
    assert params["searchDateTo"] == (today + datetime.timedelta(days=14)).isoformat()


def test_only_the_first_service_variant_is_searched():
    """The id may list several; the portal takes one."""
    client, session = searching_portal(json.dumps(payload()))

    client.search_appointments(locator("45*4502,4503*-1*-1"), lookup_days=14)

    assert session.params[0]["serviceVariantId"] == "4502"


def test_a_slot_beyond_the_lookup_window_is_dropped():
    """The portal honours searchDateTo loosely, so the window is enforced here as well."""
    far = (datetime.datetime.now() + datetime.timedelta(days=60)).replace(microsecond=0)
    near = (datetime.datetime.now() + datetime.timedelta(days=2)).replace(microsecond=0)
    body = json.dumps(payload(
        {**term(), "dateTimeFrom": far.isoformat()},
        {**term(), "dateTimeFrom": near.isoformat()},
    ))
    client, _ = searching_portal(body)

    found = client.search_appointments(locator(), lookup_days=14)

    assert len(found) == 1, "the slot 60 days out is outside a 14 day lookup"
    assert found[0].AppointmentDate.date() == near.date()
