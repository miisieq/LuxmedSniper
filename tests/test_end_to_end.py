"""Acceptance tests: whole cycles, driven the way the CLI drives them.

These are the behaviours worth protecting across refactors - secret redaction, the HTTP log
line, outage escalation, the heartbeat - so they go through CycleRunner rather than through any
single module. Per-module unit tests live in the other test_*.py files.
"""

import time

import pytest
import requests
from conftest import (
    LOGIN_BODY,
    MAINTENANCE_PAGE,
    OUTAGE_PAGE,
    PASSWORD,
    PUSHOVER_API_TOKEN,
    PUSHOVER_USER_KEY,
    SESSION_TOKEN,
    FakeResponse,
    terms,
)
from loguru import logger

from luxmedsniper import alerts, portal
from luxmedsniper import config as config_module
from luxmedsniper import runner as runner_module

LOGIN_URL = portal.LuxMedPortal.LUXMED_LOGIN_URL
SEARCH_URL = portal.LuxMedPortal.NEW_PORTAL_RESERVATION_URL


def run_cycles(path, count=1, delay=900):
    """One runner, several cycles - the way the process actually runs.

    The consecutive-failure count lives on the runner and has to survive between cycles, which
    is exactly what the escalation tests below depend on.
    """
    runner = runner_module.CycleRunner([path], delay)
    for _ in range(count):
        runner.run_cycle()
    return runner


# --- secrets ---------------------------------------------------------------------------------


def test_config_dump_masks_every_secret(http, log, config):
    config_module.load_configuration([config()])
    captured = log.getvalue()

    assert "***" in captured
    for secret in (PASSWORD, PUSHOVER_USER_KEY, PUSHOVER_API_TOKEN):
        assert secret not in captured
    assert "jan" in captured, "the login is not a secret and stays readable"


def test_network_error_does_not_print_credentials(http, config, capsys):
    """loguru's diagnose= renders locals into tracebacks; login() has the credentials in scope."""
    http.login_plan = lambda n: (_ for _ in ()).throw(requests.exceptions.ConnectionError("down"))
    alerts.setup_logging()  # the production handler configuration is what is under test

    run_cycles(config())

    captured = capsys.readouterr().out
    assert PASSWORD not in captured
    assert "ConnectionError" in captured, "the traceback must stay useful"
    assert "portal.py" in captured, "the frame that failed must be named"


def test_successful_login_body_is_skipped(http, log, config):
    run_cycles(config())
    captured = log.getvalue()

    assert SESSION_TOKEN not in captured
    assert "<skipped:" in captured


def test_failed_login_body_is_readable(http, log, config):
    """A non-200 login carries no token, and its body says whether this is maintenance."""
    http.login_plan = lambda n: FakeResponse("POST", LOGIN_URL, 503, MAINTENANCE_PAGE, "text/html")

    run_cycles(config())
    captured = log.getvalue()

    assert "Przerwa techniczna" in captured
    assert "<skipped:" not in captured


# --- HTTP logging ----------------------------------------------------------------------------


def test_http_line_carries_method_status_and_type(http, log, config):
    run_cycles(config())
    lines = [line for line in log.getvalue().splitlines() if "HTTP " in line]

    assert len(lines) == 2, "one login and one search"
    assert any("HTTP POST" in line and "-> 200" in line for line in lines)
    assert any("HTTP GET" in line and "application/json" in line for line in lines)


def test_long_body_is_truncated_on_one_line(http, log, config):
    body = terms(40)
    assert len(body) > portal.LOG_BODY_MAX_CHARS
    http.search_plan = lambda n: FakeResponse("GET", SEARCH_URL, body=body)

    run_cycles(config())

    expected = f"(+{len(body) - portal.LOG_BODY_MAX_CHARS} chars)"
    assert expected in log.getvalue()


# --- authentication --------------------------------------------------------------------------


def test_browser_headers_are_sent_and_cookies_are_not_duplicated(http, log, config):
    run_cycles(config())
    sent = http.headers_sent[-1]

    # Literal, not a loop over BROWSER_HEADERS: reading the expectation from the constant
    # under test means an empty or wrong constant is unobservable.
    assert sent.get("X-Requested-With") == "XMLHttpRequest", (
        "without it an expired session answers 200 with a login page instead of 401"
    )
    assert sent.get("Accept", "").startswith("application/json")
    assert "Mozilla/" in sent.get("User-Agent", "")
    for cookie in ("Authorization-Token", "ASP.NET_SessionId", "authorization-token"):
        assert cookie not in sent, "cookies authenticate through the Cookie header alone"


def test_one_login_per_cycle(http, log, config):
    run_cycles(config(), count=5)

    assert http.logins == 5
    assert http.searches == 5


def test_rejected_session_raises_instead_of_parsing_a_login_page(http, log, config):
    http.search_plan = lambda n: FakeResponse("GET", SEARCH_URL, 401, "Unauthorized", "text/plain")

    run_cycles(config())

    assert "Session rejected by the portal (401)" in log.getvalue()
    assert http.searches == 1, "no retry - the next cycle logs in anew"


def test_config_is_reread_every_cycle(http, log, config):
    path = config()
    runner = runner_module.CycleRunner([path], 900)
    runner.run_cycle()
    config(locators="- {id: 45*4502*-1*67013, name: Internist}\n    - {id: 45*4565*-1*55312, name: Second}")
    runner.run_cycle()

    assert http.searches == 3, "one search in the first cycle, two in the second"
    assert "Second" in log.getvalue()


# --- notifications ---------------------------------------------------------------------------


def test_known_appointments_are_not_announced_twice(http, log, config):
    run_cycles(config(), count=3)
    captured = log.getvalue()

    assert captured.count("Notification sent for") == 1
    assert captured.count("Notification was already sent") == 2


# --- failure escalation ----------------------------------------------------------------------


@pytest.fixture
def failing(http):
    http.login_plan = lambda n: FakeResponse("POST", LOGIN_URL, 503, OUTAGE_PAGE, "text/html")
    return http


def _levels(captured):
    return [line.split("|")[0] for line in captured.splitlines() if line.split("|")[0] in
            ("WARNING", "CRITICAL", "SUCCESS")]


def test_escalates_to_critical_only_once_per_outage(failing, log, config):
    run_cycles(config(), count=12)
    levels = _levels(log.getvalue())

    assert levels[:4] == ["WARNING", "WARNING", "CRITICAL", "WARNING"]
    assert levels.count("CRITICAL") == 1, "an alert per cycle would be a flood, not a signal"


def test_alert_reports_elapsed_time_from_the_first_failure(failing, log, config):
    run_cycles(config(), count=3)

    assert "~30 min" in log.getvalue(), "the third failure is two intervals after the first"


def test_alert_names_the_http_status_and_label(failing, log, config):
    run_cycles(config(), count=3)

    assert "Last failure: HTTP 503 Service Unavailable" in log.getvalue()


def test_alert_names_the_exception_when_there_was_no_response(http, log, config):
    http.login_plan = lambda n: (_ for _ in ()).throw(requests.exceptions.ConnectionError("down"))
    run_cycles(config(), count=3)

    assert "Last failure: ConnectionError" in log.getvalue()


def test_alert_reports_the_most_recent_cause(http, log, config):
    def plan(n):
        if n < 3:
            raise requests.exceptions.ConnectionError("down")
        return FakeResponse("POST", LOGIN_URL, 503, OUTAGE_PAGE, "text/html")

    http.login_plan = plan
    run_cycles(config(), count=3)

    assert "Last failure: HTTP 503" in log.getvalue()


def test_disabled_locators_are_not_an_outage(http, log, config):
    path = config(locators="- {id: 45*4502*-1*67013, name: Internist, enabled: False}")
    run_cycles(path, count=5)

    assert http.searches == 0, "a disabled locator must not reach the portal at all"
    assert _levels(log.getvalue()) == []
    assert http.pushes == []
    assert (config.tmp_path / "heartbeat").exists(), "doing nothing on purpose is still healthy"


# --- heartbeat -------------------------------------------------------------------------------


def test_heartbeat_records_a_deadline_three_intervals_ahead(http, log, config):
    """The 3x window is what HEALTHCHECK --interval=60s --retries=2 in the Dockerfile relies on,
    and it matches FAILED_CYCLES_BEFORE_CRITICAL so the alert and the unhealthy flag coincide."""
    run_cycles(config(), delay=900)
    deadline = float((config.tmp_path / "heartbeat").read_text())

    assert deadline == pytest.approx(time.time() + 900 * 3, abs=5)


def test_heartbeat_is_not_written_when_nothing_reached_the_portal(failing, log, config):
    run_cycles(config())

    assert not (config.tmp_path / "heartbeat").exists()


# --- Pushover sink ---------------------------------------------------------------------------


def _with_sink(config_path):
    alerts.add_critical_alert_sink(config_module.load_configuration([config_path]))


def test_outage_and_recovery_each_send_one_push(http, log, config):
    path = config()
    _with_sink(path)
    http.login_plan = lambda n: (
        FakeResponse("POST", LOGIN_URL, 503, OUTAGE_PAGE, "text/html")
        if n <= 3
        else FakeResponse("POST", LOGIN_URL, body=LOGIN_BODY)
    )

    run_cycles(path, count=4)

    assert [push["priority"] for push in http.pushes] == [1, 0]
    assert [push["title"] for push in http.pushes] == ["Awaria LUX MED Sniper", "LUX MED Sniper znów działa"]
    assert "Komunikacja z LuxMedem przywrócona" in http.pushes[1]["message"]
    assert "Brak kontaktu z portalem LuxMed" in http.pushes[0]["message"]
    assert "Ostatni błąd: HTTP 503 Service Unavailable" in http.pushes[0]["message"]
    # The phone speaks Polish, the log stays English - neither may borrow from the other.
    captured = log.getvalue()
    assert "No search has reached the portal" in captured
    assert "Talking to LuxMed again" in captured
    assert "Brak kontaktu z portalem LuxMed" not in captured
    assert "Komunikacja z LuxMedem" not in captured


def test_recovery_without_an_announced_outage_stays_quiet(http, log, config):
    path = config()
    _with_sink(path)
    http.login_plan = lambda n: (
        FakeResponse("POST", LOGIN_URL, 503, OUTAGE_PAGE, "text/html")
        if n == 1
        else FakeResponse("POST", LOGIN_URL, body=LOGIN_BODY)
    )

    run_cycles(path, count=3)

    assert http.pushes == [], "nobody was told about an outage, so there is nothing to recover from"


def test_maintenance_sends_a_notice_at_its_start_and_its_end(http, log, config):
    path = config()
    _with_sink(path)
    http.login_plan = lambda n: (
        FakeResponse("POST", LOGIN_URL, 503, MAINTENANCE_PAGE, "text/html")
        if n <= 5
        else FakeResponse("POST", LOGIN_URL, body=LOGIN_BODY)
    )

    run_cycles(path, count=6)

    assert [push["title"] for push in http.pushes] == ["Przerwa techniczna LUX MED", "LUX MED Sniper znów działa"]
    assert [push["priority"] for push in http.pushes] == [0, 0], "planned work wakes nobody"
    assert "CRITICAL" not in _levels(log.getvalue())
    assert (config.tmp_path / "heartbeat").exists()


def test_maintenance_that_begins_after_login_is_recognised(http, log, config):
    path = config()
    _with_sink(path)
    http.search_plan = lambda n: FakeResponse("GET", SEARCH_URL, 503, MAINTENANCE_PAGE, "text/html")

    run_cycles(path, count=4)

    assert [push["title"] for push in http.pushes] == ["Przerwa techniczna LUX MED"]


def test_healthy_cycles_send_nothing(http, log, config):
    path = config()
    _with_sink(path)
    run_cycles(path, count=3)

    assert http.pushes == []


def test_sink_is_not_installed_without_pushover_keys(http, log, tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        f"luxmed: {{login: jan, password: {PASSWORD}}}\n"
        "pushover: {user_key: , api_token: }\n"
        "luxmedsniper: {doctor_locators: [], lookup_time_days: 14, notification_provider: [console]}\n"
        "console: {message_template: 'x'}\n"
        f"misc: {{notifydb: {tmp_path / 'n.db'}}}\n",
        encoding="utf-8",
    )
    alerts.add_critical_alert_sink(config_module.load_configuration([str(path)]))

    logger.critical("boom")

    assert http.pushes == []
    assert "Pushover not configured" in log.getvalue()
