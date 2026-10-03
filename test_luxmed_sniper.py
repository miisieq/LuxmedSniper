"""Tests for luxmed_sniper.

Nothing here touches the network: requests.Session.request and requests.post are stubbed in
every test through the `http` fixture. Run with `pytest` from the repository root.
"""

import datetime
import io
import json
import time

import pytest
import requests
from loguru import logger

import luxmed_sniper as ls

PASSWORD = "PASSWORD_THAT_MUST_NOT_LEAK"
PUSHOVER_USER_KEY = "USER_KEY_THAT_MUST_NOT_LEAK"
PUSHOVER_API_TOKEN = "API_TOKEN_THAT_MUST_NOT_LEAK"
SESSION_TOKEN = "TOKEN_THAT_MUST_NOT_LEAK"

LOGIN_BODY = json.dumps({"token": SESSION_TOKEN})
MAINTENANCE_PAGE = "<html><body>Przerwa techniczna</body></html>" + "x" * 900


def _terms(count: int) -> str:
    """A terms/index payload with `count` slots, all for doctor 67013 so the filter keeps them.

    The date has to fall inside lookup_time_days, otherwise the client-side filter drops it.
    """
    soon = datetime.datetime.now() + datetime.timedelta(days=2)
    term = {
        "dateTimeFrom": soon.replace(microsecond=0).isoformat(),
        "clinicId": 1,
        "clinicGroupId": 1,
        "clinic": "ul. Testowa 1",
        "serviceId": 4502,
        "doctor": {"id": 67013, "academicTitle": "lek.", "firstName": "Jan", "lastName": "Kowalski"},
    }
    return json.dumps({"termsForService": {"termsForDays": [{"terms": [term] * count}]}})


class FakeResponse:
    def __init__(self, method, url, status=200, body="", content_type="application/json"):
        self.status_code = status
        self.text = body
        self.content = body.encode()
        self.headers = {"Content-Type": content_type}
        self.request = type("Req", (), {"method": method, "url": url})()
        self.cookies = {"Authorization-Token": "jwt", "ASP.NET_SessionId": "abc"}

    def json(self):
        return json.loads(self.text)


class Http:
    """Records every request and answers from the plans the test installs."""

    def __init__(self):
        self.logins = 0
        self.searches = 0
        self.headers_sent = []
        self.pushes = []
        self.login_plan = lambda n: FakeResponse("POST", ls.LuxMedSniper.LUXMED_LOGIN_URL, body=LOGIN_BODY)
        self.search_plan = lambda n: FakeResponse("GET", ls.LuxMedSniper.NEW_PORTAL_RESERVATION_URL, body=_terms(1))


@pytest.fixture
def http(monkeypatch):
    recorder = Http()

    def session_request(session, method, url, **kwargs):
        assert kwargs.get("timeout") == ls.REQUEST_TIMEOUT, "every request must carry a timeout"
        recorder.headers_sent.append(dict(session.headers))
        if method == "POST":
            recorder.logins += 1
            return recorder.login_plan(recorder.logins)
        recorder.searches += 1
        return recorder.search_plan(recorder.searches)

    def post(url, **kwargs):
        recorder.pushes.append(kwargs.get("data", {}))
        return type("Resp", (), {"status_code": 200, "text": "{}"})()

    monkeypatch.setattr(requests.Session, "request", session_request)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(ls, "_failed_cycles", 0)
    return recorder


@pytest.fixture
def log():
    """Captures loguru output with the production handler settings."""
    stream = io.StringIO()
    logger.remove()
    logger.add(stream, level="DEBUG", format="{level.name}|{message}", diagnose=False)
    yield stream
    logger.remove()


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Writes a config and points the heartbeat at the same directory."""
    monkeypatch.setenv("LUXMED_HEARTBEAT_FILE", str(tmp_path / "heartbeat"))

    def write(locators="- {id: 45*4502*-1*67013, name: Internist}", providers="[console]"):
        path = tmp_path / "config.yaml"
        path.write_text(
            f"luxmed: {{login: jan, password: {PASSWORD}}}\n"
            f"pushover: {{user_key: {PUSHOVER_USER_KEY}, api_token: {PUSHOVER_API_TOKEN},"
            f" title: t, message_template: 'x'}}\n"
            "luxmedsniper:\n"
            f"  doctor_locators:\n    {locators}\n"
            "  lookup_time_days: 14\n"
            f"  notification_provider: {providers}\n"
            "console: {message_template: 'x'}\n"
            f"misc: {{notifydb: {tmp_path / 'notifications.db'}}}\n",
            encoding="utf-8",
        )
        return str(path)

    write.tmp_path = tmp_path
    return write


# --- secrets ---------------------------------------------------------------------------------


def test_config_dump_masks_every_secret(http, log, config):
    ls.LuxMedSniper._load_configuration([config()])
    captured = log.getvalue()

    assert "***" in captured
    for secret in (PASSWORD, PUSHOVER_USER_KEY, PUSHOVER_API_TOKEN):
        assert secret not in captured
    assert "jan" in captured, "the login is not a secret and stays readable"


def test_network_error_does_not_print_credentials(http, config, capsys):
    """loguru's diagnose= renders locals into tracebacks; _log_in has the credentials in scope."""
    http.login_plan = lambda n: (_ for _ in ()).throw(requests.exceptions.ConnectionError("down"))
    ls.setup_logging()  # the production handler configuration is what is under test

    ls.work([config()], delay=900)

    captured = capsys.readouterr().out
    assert PASSWORD not in captured
    assert "ConnectionError" in captured, "the traceback must stay useful"
    assert "luxmed_sniper.py" in captured


def test_successful_login_body_is_skipped(http, log, config):
    ls.work([config()], delay=900)
    captured = log.getvalue()

    assert SESSION_TOKEN not in captured
    assert "<skipped:" in captured


def test_failed_login_body_is_readable(http, log, config):
    """A non-200 login carries no token, and its body says whether this is maintenance."""
    http.login_plan = lambda n: FakeResponse(
        "POST", ls.LuxMedSniper.LUXMED_LOGIN_URL, 503, MAINTENANCE_PAGE, "text/html"
    )

    ls.work([config()], delay=900)
    captured = log.getvalue()

    assert "Przerwa techniczna" in captured
    assert "<skipped:" not in captured


# --- HTTP logging ----------------------------------------------------------------------------


def test_http_line_carries_method_status_and_type(http, log, config):
    ls.work([config()], delay=900)
    lines = [line for line in log.getvalue().splitlines() if "HTTP " in line]

    assert len(lines) == 2, "one login and one search"
    assert any("HTTP POST" in line and "-> 200" in line for line in lines)
    assert any("HTTP GET" in line and "application/json" in line for line in lines)


def test_long_body_is_truncated_on_one_line(http, log, config):
    body = _terms(40)
    assert len(body) > ls.LOG_BODY_MAX_CHARS
    http.search_plan = lambda n: FakeResponse("GET", ls.LuxMedSniper.NEW_PORTAL_RESERVATION_URL, body=body)

    ls.work([config()], delay=900)

    expected = f"(+{len(body) - ls.LOG_BODY_MAX_CHARS} chars)"
    assert expected in log.getvalue()


# --- authentication --------------------------------------------------------------------------


def test_browser_headers_are_sent_and_cookies_are_not_duplicated(http, log, config):
    ls.work([config()], delay=900)
    sent = http.headers_sent[-1]

    for header, value in ls.BROWSER_HEADERS.items():
        assert sent.get(header) == value
    for cookie in ("Authorization-Token", "ASP.NET_SessionId", "authorization-token"):
        assert cookie not in sent, "cookies authenticate through the Cookie header alone"


def test_one_login_per_cycle(http, log, config):
    path = config()
    for _ in range(5):
        ls.work([path], delay=900)

    assert http.logins == 5
    assert http.searches == 5


def test_rejected_session_raises_instead_of_parsing_a_login_page(http, log, config):
    http.search_plan = lambda n: FakeResponse(
        "GET", ls.LuxMedSniper.NEW_PORTAL_RESERVATION_URL, 401, "Unauthorized", "text/plain"
    )

    ls.work([config()], delay=900)

    assert "Session rejected by the portal (401)" in log.getvalue()
    assert http.searches == 1, "no retry - the next cycle logs in anew"


def test_config_is_reread_every_cycle(http, log, config):
    path = config()
    ls.work([path], delay=900)
    config(locators="- {id: 45*4502*-1*67013, name: Internist}\n    - {id: 45*4565*-1*55312, name: Second}")
    ls.work([path], delay=900)

    assert http.searches == 3, "one search in the first cycle, two in the second"
    assert "Second" in log.getvalue()


# --- notifications ---------------------------------------------------------------------------


def test_known_appointments_are_not_announced_twice(http, log, config):
    path = config()
    for _ in range(3):
        ls.work([path], delay=900)
    captured = log.getvalue()

    assert captured.count("Notification sent for") == 1
    assert captured.count("Notification was already sent") == 2


# --- failure escalation ----------------------------------------------------------------------


@pytest.fixture
def failing(http):
    http.login_plan = lambda n: FakeResponse(
        "POST", ls.LuxMedSniper.LUXMED_LOGIN_URL, 503, MAINTENANCE_PAGE, "text/html"
    )
    return http


def _levels(captured):
    return [line.split("|")[0] for line in captured.splitlines() if line.split("|")[0] in
            ("WARNING", "CRITICAL", "SUCCESS")]


def test_escalates_to_critical_only_once_per_outage(failing, log, config):
    path = config()
    for _ in range(12):
        ls.work([path], delay=900)
    levels = _levels(log.getvalue())

    assert levels[:4] == ["WARNING", "WARNING", "CRITICAL", "WARNING"]
    assert levels.count("CRITICAL") == 1, "an alert per cycle would be a flood, not a signal"


def test_alert_reports_elapsed_time_from_the_first_failure(failing, log, config):
    path = config()
    for _ in range(3):
        ls.work([path], delay=900)

    assert "~30 min" in log.getvalue(), "the third failure is two intervals after the first"


def test_alert_names_the_http_status_and_label(failing, log, config):
    path = config()
    for _ in range(3):
        ls.work([path], delay=900)

    assert "Last failure: HTTP 503 Service Unavailable" in log.getvalue()


def test_alert_names_the_exception_when_there_was_no_response(http, log, config):
    http.login_plan = lambda n: (_ for _ in ()).throw(requests.exceptions.ConnectionError("down"))
    path = config()
    for _ in range(3):
        ls.work([path], delay=900)

    assert "Last failure: ConnectionError" in log.getvalue()


def test_alert_reports_the_most_recent_cause(http, log, config):
    def plan(n):
        if n < 3:
            raise requests.exceptions.ConnectionError("down")
        return FakeResponse("POST", ls.LuxMedSniper.LUXMED_LOGIN_URL, 503, MAINTENANCE_PAGE, "text/html")

    http.login_plan = plan
    path = config()
    for _ in range(3):
        ls.work([path], delay=900)

    assert "Last failure: HTTP 503" in log.getvalue()


@pytest.mark.parametrize(
    ("status", "expected"),
    [(503, "HTTP 503 Service Unavailable"), (401, "HTTP 401 Unauthorized"), (599, "HTTP 599")],
)
def test_describe_failure_handles_unknown_codes(status, expected):
    assert ls.describe_failure(ls.LuxmedSniperError("boom", status)) == expected


def test_describe_failure_uses_our_own_wording_but_never_a_parser_message():
    """LuxmedSniperError text is authored here; anything else may quote the config file."""
    assert ls.describe_failure(ls.LuxmedSniperError("Conflict at .luxmed")) == "Conflict at .luxmed"

    import yaml

    try:
        yaml.safe_load(f"luxmed:\n  password: {PASSWORD}\n bad indent\n")
    except yaml.YAMLError as parser_error:
        described = ls.describe_failure(parser_error)
        assert PASSWORD not in described
        assert described.endswith("Error")


def test_disabled_locators_are_not_an_outage(http, log, config):
    path = config(locators="- {id: 45*4502*-1*67013, name: Internist, enabled: False}")
    for _ in range(5):
        ls.work([path], delay=900)

    assert _levels(log.getvalue()) == []
    assert http.pushes == []
    assert (config.tmp_path / "heartbeat").exists(), "doing nothing on purpose is still healthy"


# --- heartbeat -------------------------------------------------------------------------------


def test_heartbeat_records_a_deadline_ahead_of_now(http, log, config):
    ls.work([config()], delay=900)
    deadline = float((config.tmp_path / "heartbeat").read_text())

    assert deadline > time.time()


def test_heartbeat_is_not_written_when_nothing_reached_the_portal(failing, log, config):
    ls.work([config()], delay=900)

    assert not (config.tmp_path / "heartbeat").exists()


# --- Pushover sink ---------------------------------------------------------------------------


def _with_sink(config_path):
    ls.add_critical_alert_sink(ls.LuxMedSniper._load_configuration([config_path]))


def test_outage_and_recovery_each_send_one_push(http, log, config):
    path = config()
    _with_sink(path)
    http.login_plan = lambda n: (
        FakeResponse("POST", ls.LuxMedSniper.LUXMED_LOGIN_URL, 503, MAINTENANCE_PAGE, "text/html")
        if n <= 3
        else FakeResponse("POST", ls.LuxMedSniper.LUXMED_LOGIN_URL, body=LOGIN_BODY)
    )

    for _ in range(4):
        ls.work([path], delay=900)

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
        FakeResponse("POST", ls.LuxMedSniper.LUXMED_LOGIN_URL, 503, MAINTENANCE_PAGE, "text/html")
        if n == 1
        else FakeResponse("POST", ls.LuxMedSniper.LUXMED_LOGIN_URL, body=LOGIN_BODY)
    )

    for _ in range(3):
        ls.work([path], delay=900)

    assert http.pushes == [], "nobody was told about an outage, so there is nothing to recover from"


def test_healthy_cycles_send_nothing(http, log, config):
    path = config()
    _with_sink(path)
    for _ in range(3):
        ls.work([path], delay=900)

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
    ls.add_critical_alert_sink(ls.LuxMedSniper._load_configuration([str(path)]))

    logger.critical("boom")

    assert http.pushes == []
    assert "Pushover not configured" in log.getvalue()
