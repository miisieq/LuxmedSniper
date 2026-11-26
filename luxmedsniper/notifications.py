"""Notification providers.

Every provider is a small object with a `send`, built from the config by `build_notifiers` and
called by `dispatch`. They all format their message through `format_message`, which is what makes
`{name}` and every other field behave the same way whichever channel is configured.

Each provider reads every config key it needs in its constructor, never at send time. That is
load-bearing, not style: `dispatch` swallows exceptions so one dead channel cannot stop the
others, so a key read inside `send` turns a broken config into a silently skipped notification -
and since the appointment is recorded as announced before `send` runs, it would never be retried.
Read in the constructor, the same mistake fails inside `build_notifiers`, where it aborts the
cycle and escalates to an alert.

Optional dependencies (slack_sdk, pushbullet, gi, telegram_send) live in
optional-requirements.txt, so their imports stay inside the constructors - importing them at
module level would make the whole app require packages most installs do not have.
"""

import asyncio
import subprocess
from pathlib import Path
from typing import Any, Protocol

import requests
from loguru import logger

from .models import Appointment, DoctorLocator, LuxmedSniperError

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_PROVIDER_TIMEOUT = 10
PUSHOVER_TITLE_LIMIT = 250  # characters, per the Pushover API docs
# The alert sink is more patient than a provider: it is reporting that everything else is already
# broken, so it is worth waiting longer for it to get through.
PUSHOVER_ALERT_TIMEOUT = 30
NTFY_TIMEOUT = 10


class Notifier(Protocol):
    name: str

    def send(self, locator: DoctorLocator, appointment: Appointment) -> Any: ...


WEEKDAYS_PL = ("pon", "wt", "śr", "czw", "pt", "sob", "niedz")  # indexed by datetime.weekday()


def format_message(template: str, locator: DoctorLocator, appointment: Appointment) -> str:
    """The one place a message template is rendered.

    Fields come from both the search and the slot it found. Where they collide - `service_id` is
    the only one - the appointment wins, because the template is describing the slot that was
    actually found, not the search that found it.

    Called from `send` rather than hoisted into `dispatch` on purpose: a typo in one provider's
    template must break that provider only, not every channel configured.
    """
    fields = {
        **vars(locator),
        **vars(appointment),
        "weekday": WEEKDAYS_PL[appointment.AppointmentDate.weekday()],  # %a would follow the locale
    }
    return template.format(**fields)


def pushover_push(
    credentials: dict[str, Any],
    *,
    title: str,
    message: str,
    timeout: int,
    priority: int | None = None,
) -> requests.Response:
    """Posts to Pushover and hands the response back without judging it.

    Deliberately does not raise: its two callers want different things. The provider turns a
    non-200 into an exception, while the alert sink only logs - a raise there would happen inside
    a loguru handler, which prints a "Logging error in Loguru Handler" block instead of the clean
    warning, and the warning itself is kept below CRITICAL so it cannot re-enter that same sink.
    """
    data = {
        "token": credentials["api_token"],
        "user": credentials["user_key"],
        "message": message,
        "title": title[:PUSHOVER_TITLE_LIMIT],  # a refused push is never retried
    }
    if priority is not None:
        data["priority"] = priority
    return requests.post(PUSHOVER_URL, data=data, timeout=timeout)


class PushoverNotifier:
    name = "pushover"

    def __init__(self, settings: dict[str, Any]) -> None:
        self._credentials = {
            "api_token": settings["api_token"],
            "user_key": settings["user_key"],
        }
        self._title = settings["title"]
        self._template = settings["message_template"]

    def send(self, locator: DoctorLocator, appointment: Appointment) -> None:
        response = pushover_push(
            self._credentials,
            title=format_message(self._title, locator, appointment),  # so it can carry {name}
            message=format_message(self._template, locator, appointment),
            timeout=PUSHOVER_PROVIDER_TIMEOUT,
        )
        if response.status_code != requests.codes["ok"]:
            err_msg = f"Pushover error: {response.text}"
            raise LuxmedSniperError(err_msg)


class NtfyNotifier:
    name = "ntfy"

    def __init__(self, settings: dict[str, Any]) -> None:
        self._url = f"https://ntfy.sh/{settings['topic']}"
        self._template = settings["message_template"]

    def send(self, locator: DoctorLocator, appointment: Appointment) -> None:
        requests.post(
            self._url,
            data=format_message(self._template, locator, appointment),
            headers={"Tags": "hospital,pill,syringe", "Title": "Nowa wizyta"},
            timeout=NTFY_TIMEOUT,
        )


class SlackNotifier:
    name = "slack"

    def __init__(self, settings: dict[str, Any]) -> None:
        token = settings["api_token"]
        self._channel = settings["channel"]
        self._template = settings["message_template"]

        from slack_sdk import WebClient  # noqa: PLC0415

        self._client = WebClient(token=token)

    def send(self, locator: DoctorLocator, appointment: Appointment) -> None:
        self._client.chat_postMessage(
            channel=self._channel,
            text=format_message(self._template, locator, appointment),
        )


class PushbulletNotifier:
    name = "pushbullet"

    def __init__(self, settings: dict[str, Any]) -> None:
        access_token = settings["access_token"]
        self._title = settings["title"]
        self._template = settings["message_template"]

        from pushbullet import Pushbullet  # noqa: PLC0415

        self._pushbullet = Pushbullet(access_token)

    def send(self, locator: DoctorLocator, appointment: Appointment) -> None:
        self._pushbullet.push_note(
            title=self._title,
            body=format_message(self._template, locator, appointment),
        )


class TelegramNotifier:
    name = "telegram"

    def __init__(self, settings: dict[str, Any]) -> None:
        self._conf_path = settings["tele_conf_path"]
        self._template = settings["message_template"]

        from telegram_send import send as telegram_send  # noqa: PLC0415

        self._send = telegram_send

    def send(self, locator: DoctorLocator, appointment: Appointment) -> None:
        self._send(
            messages=[format_message(self._template, locator, appointment)],
            conf=self._conf_path,
        )


class LibnotifyNotifier:
    name = "gi"

    def __init__(self, settings: dict[str, Any]) -> None:
        self._template = settings["message_template"]

        import gi  # noqa: PLC0415

        gi.require_version("Notify", "0.7")
        from gi.repository import Notify  # noqa: PLC0415

        Notify.init("Luxmed Sniper")  # one time initialization of libnotify
        self._notify = Notify

    def send(self, locator: DoctorLocator, appointment: Appointment) -> None:
        message = format_message(self._template, locator, appointment)
        self._notify.Notification.new(message, None).show()


class SoundNotifier:
    name = "sound"

    def __init__(self, settings: dict[str, Any]) -> None:
        self._audio_file = Path(settings["audio"])

    def send(self, locator: DoctorLocator, appointment: Appointment) -> None:
        subprocess.check_output(  # noqa: S603
            ["/usr/bin/aplay", self._audio_file],
            shell=False,
            stderr=subprocess.STDOUT,
        )


class ConsoleNotifier:
    name = "console"

    def __init__(self, settings: dict[str, Any]) -> None:
        self._template = settings["message_template"]

    def send(self, locator: DoctorLocator, appointment: Appointment) -> None:
        print(format_message(self._template, locator, appointment))


class AsyncConsoleNotifier:
    """Same as ConsoleNotifier, but its `send` is a coroutine - `dispatch` runs it."""

    name = "console_async"

    def __init__(self, settings: dict[str, Any]) -> None:
        self._template = settings["message_template"]

    async def send(self, locator: DoctorLocator, appointment: Appointment) -> None:
        print(format_message(self._template, locator, appointment))


# Config section name -> provider. The section name doubles as the key the provider reads its
# own settings from, which is why a single mapping is enough.
NOTIFIERS: dict[str, type[Notifier]] = {
    "pushover": PushoverNotifier,
    "ntfy": NtfyNotifier,
    "slack": SlackNotifier,
    "pushbullet": PushbulletNotifier,
    "telegram": TelegramNotifier,
    "gi": LibnotifyNotifier,
    "sound": SoundNotifier,
    "console": ConsoleNotifier,
    "console_async": AsyncConsoleNotifier,
}


def build_notifiers(config: dict[str, Any]) -> list[Notifier]:
    """Builds every selected provider, failing here rather than at send time.

    An unknown name is a warning, not an error: rejecting it would be config validation, which
    this project deliberately does not do. But it must be said out loud - silently dropping a
    misspelled provider leaves a cycle that looks perfectly healthy and notifies nobody.
    """
    notifiers = []
    for name in config["luxmedsniper"]["notification_provider"]:
        if name not in NOTIFIERS:
            logger.warning(
                "Unknown notification_provider {!r}, skipping it. Known providers: {}",
                name,
                ", ".join(NOTIFIERS),
            )
            continue
        notifiers.append(NOTIFIERS[name](config[name]))
    return notifiers


def dispatch(notifiers: list[Notifier], locator: DoctorLocator, appointment: Appointment) -> None:
    """Runs every provider. One failing provider must not stop the others."""
    for notifier in notifiers:
        try:
            result = notifier.send(locator, appointment)
            if result is not None and asyncio.iscoroutine(result):
                asyncio.run(result)
        except Exception as e:
            # Naming the provider matters with several configured: without it the log says
            # something broke but not which channel the user is no longer hearing from.
            logger.exception("Notifier {} failed: {}", notifier.name, e)
