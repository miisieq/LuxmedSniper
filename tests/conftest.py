"""Shared fixtures and helpers.

Nothing in the suite touches the network: `requests.Session.request` and `requests.post` are
stubbed by the `http` fixture. Run with `pytest` from the repository root.
"""

import datetime
import io
import json
import logging

import pytest
import requests
from loguru import logger

from luxmedsniper import portal

PASSWORD = "PASSWORD_THAT_MUST_NOT_LEAK"
PUSHOVER_USER_KEY = "USER_KEY_THAT_MUST_NOT_LEAK"
PUSHOVER_API_TOKEN = "API_TOKEN_THAT_MUST_NOT_LEAK"
SESSION_TOKEN = "TOKEN_THAT_MUST_NOT_LEAK"

LOGIN_BODY = json.dumps({"token": SESSION_TOKEN})
MAINTENANCE_PAGE = "<html><head><title>Przerwa techniczna</title></head><body>" + "x" * 900 + "</body></html>"
OUTAGE_PAGE = "<html><body>Service Unavailable</body></html>" + "x" * 900  # a 503 that is not the planned break


def terms(count: int) -> str:
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
        self.login_plan = lambda n: FakeResponse("POST", portal.LuxMedPortal.LUXMED_LOGIN_URL, body=LOGIN_BODY)
        self.search_plan = lambda n: FakeResponse(
            "GET", portal.LuxMedPortal.NEW_PORTAL_RESERVATION_URL, body=terms(1),
        )


@pytest.fixture
def http(monkeypatch):
    recorder = Http()

    def session_request(session, method, url, **kwargs):
        assert kwargs.get("timeout") == portal.REQUEST_TIMEOUT, "every request must carry a timeout"
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

    def write(locators="- {id: 45*4502*-1*67013, name: Internist}"):
        path = tmp_path / "config.yaml"
        path.write_text(
            f"luxmed: {{login: jan, password: {PASSWORD}}}\n"
            f"pushover: {{user_key: {PUSHOVER_USER_KEY}, api_token: {PUSHOVER_API_TOKEN},"
            f" title: t, message_template: 'x'}}\n"
            "luxmedsniper:\n"
            f"  doctor_locators:\n    {locators}\n"
            "  lookup_time_days: 14\n"
            "  notification_provider: [console]\n"
            "console: {message_template: 'x'}\n"
            f"misc: {{notifydb: {tmp_path / 'notifications.db'}}}\n",
            encoding="utf-8",
        )
        return str(path)

    write.tmp_path = tmp_path
    return write


@pytest.fixture(autouse=True)
def _isolate_global_logging_state():
    """Gives every test a clean logging world and takes back whatever it installed.

    Logging is process-wide on both sides - loguru keeps its handlers in a module-level
    registry, and `setup_logging` additionally reconfigures the stdlib root logger - so a test
    that installs a sink would otherwise leak it into every test that follows.

    Handlers are cleared *before* the test as well: a leaked loguru stdout sink holds the
    `sys.stdout` that was current when it was added, so a later test using capsys would have its
    output written to the previous test's captured stream.
    """
    urllib3 = logging.getLogger("urllib3")
    saved = (logging.root.handlers[:], logging.root.level, urllib3.level, urllib3.propagate)

    logger.remove()
    yield
    logger.remove()

    logging.root.handlers[:], logging.root.level, urllib3.level, urllib3.propagate = saved
