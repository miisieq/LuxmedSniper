"""Process-wide logging setup and the Pushover channel for outage alerts.

Both are start-up wiring, called once from the CLI - not part of a cycle, which is why they do
not live in runner.py.
"""

import inspect
import logging
import os
import sys
from typing import Any

import requests
from loguru import logger

from .notifications import PUSHOVER_ALERT_TIMEOUT, pushover_push


def setup_logging() -> None:
    class InterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # Get corresponding Loguru level if it exists.
            level: str | int
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            # Find caller from where originated the logged message.
            frame, depth = inspect.currentframe(), 0
            while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
                frame = frame.f_back
                depth += 1

            logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    requests_log = logging.getLogger("urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True

    # stdout only: whatever runs the process persists it - docker logs, journald, a shell
    # redirect. A second file sink would just duplicate this at the same level.
    handlers = [
        # diagnose=False: loguru otherwise appends the values of variables in scope to every
        # traceback frame, and a network error inside login() would print the credentials dict.
        dict(sink=sys.stdout, level=os.environ.get("LOGURU_LEVEL", "INFO"), diagnose=False),
    ]
    logger.configure(handlers=handlers)


def add_critical_alert_sink(config: dict[str, Any]) -> int | None:
    """Routes CRITICAL records, and anything bound with alert=True, to Pushover.

    A loguru sink rather than a call in each failure path, so any future logger.critical reaches
    the phone without extra plumbing. Notification providers cannot serve this: they are called
    with an Appointment, and a dead login has none. loguru's own catch= keeps a broken alert
    channel from taking the process down - the record still reaches stdout either way.

    Returns the handler id, so a caller can take the sink off again.
    """
    pushover = config.get("pushover") or {}
    if not (pushover.get("user_key") and pushover.get("api_token")):
        logger.info("Pushover not configured, CRITICAL records stay in the log only")
        return None

    def sink(message: Any) -> None:
        record = message.record
        is_outage = record["level"].name == "CRITICAL"
        response = pushover_push(
            pushover,
            title="Awaria LUX MED Sniper" if is_outage else "LUX MED Sniper znów działa",
            # The phone speaks Polish, the log stays English. A record without a Polish
            # version still gets pushed, just in the log's wording.
            message=record["extra"].get("pushover") or record["message"],
            # Outages bypass quiet hours; "it works again" has no business waking anyone.
            priority=1 if is_outage else 0,
            timeout=PUSHOVER_ALERT_TIMEOUT,
        )
        # Below CRITICAL on purpose: a warning here cannot re-enter this sink.
        if response.status_code != requests.codes["ok"]:
            logger.warning("Pushover rejected the alert: {} {}", response.status_code, response.text)
        else:
            logger.info("Alert sent to Pushover")

    # level= alone cannot express this: SUCCESS sits below WARNING, so the threshold that lets
    # the recovery notice through would also forward every warning. The filter picks the two
    # events worth a push - an outage, and the recovery that ends it.
    handler_id = logger.add(
        sink,
        level="SUCCESS",
        filter=lambda record: record["level"].name == "CRITICAL" or record["extra"].get("alert"),
    )
    logger.info("Outage and recovery notices will also be sent to Pushover")
    return handler_id
