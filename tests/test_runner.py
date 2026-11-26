"""The search loop and the outage escalation, with no network and no files."""

import datetime

import pytest

from luxmedsniper.models import Appointment, CheckResult, DoctorLocator, LocatorSet, LuxmedSniperError
from luxmedsniper.runner import FAILED_CYCLES_BEFORE_CRITICAL, CycleRunner, run_checks


def locator(name="Internist", *, enabled=True):
    return DoctorLocator.model_validate({"id": "45*4502*-1*-1", "name": name, "enabled": enabled})


def appointment(doctor="lek. Jan Kowalski"):
    return Appointment(
        datetime.datetime(2026, 10, 9, 14, 30),
        clinic_id=1,
        ClinicPublicName="ul. Testowa 1",
        DoctorName=doctor,
        service_id=4502,
    )


class FakePortal:
    def __init__(self, result):
        self._result = result
        self.searched = []

    def search_appointments(self, loc, lookup_days):
        self.searched.append(loc.name)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeStore:
    def __init__(self, known=()):
        self.known = list(known)
        self.remembered = []

    def is_known(self, appt):
        return appt.DoctorName in self.known

    def remember(self, appt):
        self.remembered.append(appt.DoctorName)


class FakeNotifier:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, loc, appt):
        self.sent.append(appt.DoctorName)


def check(portal, locators, store=None, notifiers=None, errors=()):
    locator_set = LocatorSet(locators=list(locators), errors=list(errors))
    return run_checks(portal, locator_set, store or FakeStore(), notifiers or [], 14)


# --- run_checks ------------------------------------------------------------------------------


def test_a_found_appointment_is_remembered_and_announced():
    store, notifier = FakeStore(), FakeNotifier()

    result = check(FakePortal([appointment()]), [locator()], store, [notifier])

    assert result == CheckResult(enabled=1, searched=1, last_error="")
    assert store.remembered == ["lek. Jan Kowalski"]
    assert notifier.sent == ["lek. Jan Kowalski"]


def test_a_known_appointment_is_not_announced_again():
    store, notifier = FakeStore(known=["lek. Jan Kowalski"]), FakeNotifier()

    check(FakePortal([appointment()]), [locator()], store, [notifier])

    assert store.remembered == []
    assert notifier.sent == []


def test_an_appointment_is_recorded_before_it_is_sent():
    """A provider that fails must not cause the same slot to be announced again next cycle.

    Asserting only that the slot ends up remembered proves nothing - `dispatch` swallows the
    provider's exception, so that holds whichever order the two calls run in. The notifier has
    to observe the store at the moment it is called.
    """
    store = FakeStore()
    observed = []

    class Observing:
        name = "observing"

        def send(self, loc, appt):
            observed.append(list(store.remembered))

    check(FakePortal([appointment()]), [locator()], store, [Observing()])

    assert observed == [["lek. Jan Kowalski"]], "the slot must already be recorded when send runs"


def test_an_empty_schedule_still_counts_as_having_reached_the_portal():
    assert check(FakePortal([]), [locator()]) == CheckResult(enabled=1, searched=1, last_error="")


def test_disabled_locators_are_neither_enabled_nor_searched():
    portal = FakePortal([])

    result = check(portal, [locator("Off", enabled=False)])

    assert result == CheckResult(enabled=0, searched=0, last_error="")
    assert portal.searched == []


def test_one_broken_search_does_not_stop_the_others():
    class HalfBroken(FakePortal):
        def search_appointments(self, loc, lookup_days):
            self.searched.append(loc.name)
            if loc.name == "First":
                msg = "nope"
                raise LuxmedSniperError(msg, 503)
            return []

    portal = HalfBroken([])
    result = check(portal, [locator("First"), locator("Second")])

    assert portal.searched == ["First", "Second"]
    assert result.enabled == 2
    assert result.searched == 1
    assert result.last_error == "HTTP 503 Service Unavailable"


def test_the_last_error_is_the_most_recent_one():
    class Failing(FakePortal):
        def search_appointments(self, loc, lookup_days):
            raise LuxmedSniperError("nope", 503 if loc.name == "First" else 401)

    result = check(Failing([]), [locator("First"), locator("Second")])

    assert result.last_error == "HTTP 401 Unauthorized"


# --- escalation ------------------------------------------------------------------------------


class StubRunner(CycleRunner):
    """A runner whose cycle outcome the test dictates, so nothing is loaded or logged in."""

    def __init__(self, results, delay=900):
        super().__init__(["unused.yaml"], delay)
        self._results = list(results)

    def run_cycle(self):
        self._next = self._results.pop(0)
        super().run_cycle()


def levels(captured):
    wanted = ("WARNING", "CRITICAL", "SUCCESS")
    return [line.split("|")[0] for line in captured.splitlines() if line.split("|")[0] in wanted]


@pytest.fixture
def runner_of(monkeypatch):
    def build(results, delay=900):
        runner = StubRunner(results, delay)
        monkeypatch.setattr(runner, "_collaborators", lambda: ())
        monkeypatch.setattr("luxmedsniper.runner.run_checks", lambda *a: runner._next)
        monkeypatch.setattr("luxmedsniper.runner.write_heartbeat", lambda delay: None)
        return runner

    return build


OK = CheckResult(enabled=1, searched=1)
FAIL = CheckResult(enabled=1, searched=0, last_error="HTTP 503 Service Unavailable")
IDLE = CheckResult(enabled=0, searched=0)


def test_the_counter_survives_between_cycles(runner_of):
    runner = runner_of([FAIL, FAIL, FAIL])
    for _ in range(3):
        runner.run_cycle()

    assert runner.failed_cycles == FAILED_CYCLES_BEFORE_CRITICAL


def test_the_third_consecutive_failure_escalates_once(runner_of, log):
    runner = runner_of([FAIL] * 5)
    for _ in range(5):
        runner.run_cycle()

    assert levels(log.getvalue()) == ["WARNING", "WARNING", "CRITICAL", "WARNING", "WARNING"]


def test_a_success_resets_the_counter(runner_of):
    runner = runner_of([FAIL, FAIL, OK])
    for _ in range(3):
        runner.run_cycle()

    assert runner.failed_cycles == 0


@pytest.fixture
def pushover_texts(log):
    """The Polish wording is bound to the record rather than logged, so it needs its own sink.

    That split is the point: the log stays English and only the phone gets Polish.

    Depends on `log` on purpose: that fixture's first act is `logger.remove()`, so without the
    dependency this sink would survive or not depending on parameter order in the signature.
    """
    from loguru import logger

    captured = []
    handler_id = logger.add(
        lambda m: captured.append(m.record["extra"].get("pushover")), level="SUCCESS",
    )
    yield captured
    logger.remove(handler_id)


def test_recovery_after_an_announced_outage_is_pushed(runner_of, log, pushover_texts):
    runner = runner_of([FAIL, FAIL, FAIL, OK])
    for _ in range(4):
        runner.run_cycle()

    assert "Talking to LuxMed again" in log.getvalue(), "the log stays English"
    polish = [text for text in pushover_texts if text]
    assert any("Komunikacja z LuxMedem przywrócona" in text for text in polish), "the phone gets Polish"
    assert "Komunikacja z LuxMedem" not in log.getvalue(), "and the log never borrows it"


def test_recovery_without_an_announced_outage_is_only_logged(runner_of, log, pushover_texts):
    runner = runner_of([FAIL, OK])
    for _ in range(2):
        runner.run_cycle()

    assert "Recovered after 1 failed cycle(s)" in log.getvalue()
    assert not [t for t in pushover_texts if t], "nobody was told there was an outage"


def test_everything_disabled_is_not_an_outage(runner_of, log):
    runner = runner_of([IDLE] * 5)
    for _ in range(5):
        runner.run_cycle()

    assert levels(log.getvalue()) == []
    assert runner.failed_cycles == 0


def test_the_alert_names_the_last_error_and_the_elapsed_time(runner_of, log):
    runner = runner_of([FAIL] * 3)
    for _ in range(3):
        runner.run_cycle()

    captured = log.getvalue()
    assert "Last failure: HTTP 503 Service Unavailable" in captured
    assert "~30 min" in captured, "two intervals since the first failure"


# --- locators that would not parse -----------------------------------------------------------


def test_an_unparseable_locator_counts_as_enabled_but_not_searched():
    """Otherwise a config whose every locator is malformed looks like one with every search off."""
    result = check(FakePortal([]), [], errors=["locator 'BROKEN': bad format"])

    assert result == CheckResult(enabled=1, searched=0, last_error="locator 'BROKEN': bad format")


def test_working_locators_are_still_searched_alongside_a_broken_one():
    portal = FakePortal([])

    result = check(portal, [locator("Good")], errors=["locator 'BROKEN': bad format"])

    assert portal.searched == ["Good"]
    assert result.enabled == 2
    assert result.searched == 1, "one search worked, so the cycle is not an outage"


def test_a_config_whose_every_locator_is_broken_escalates(runner_of, log):
    broken = CheckResult(enabled=2, searched=0, last_error="locator 'BROKEN': bad format")
    runner = runner_of([broken] * 3)
    for _ in range(3):
        runner.run_cycle()

    assert levels(log.getvalue()) == ["WARNING", "WARNING", "CRITICAL"]
    assert "locator 'BROKEN'" in log.getvalue()


# --- cycle wiring ----------------------------------------------------------------------------


def test_the_portal_login_happens_after_everything_the_config_alone_can_break(monkeypatch, tmp_path):
    """A bad provider section must not cost a credentialed login - LUX MED locks accounts for
    hammering, and a config that cannot work would otherwise log in once per cycle forever."""
    order = []

    config_file = tmp_path / "c.yaml"
    config_file.write_text(
        "luxmed: {login: jan, password: pw}\n"
        "luxmedsniper: {doctor_locators: [], lookup_time_days: 14, notification_provider: []}\n"
        f"misc: {{notifydb: {tmp_path / 'n.db'}}}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("luxmedsniper.runner.build_notifiers", lambda config: order.append("notifiers") or [])
    monkeypatch.setattr("luxmedsniper.runner.LuxMedPortal.login", lambda self: order.append("login"))

    CycleRunner([str(config_file)], 900)._collaborators()

    assert order == ["notifiers", "login"]
