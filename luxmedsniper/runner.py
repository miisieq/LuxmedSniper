"""One search cycle, and the outage tracking that spans cycles."""

import os
import time
from pathlib import Path
from typing import Any

from loguru import logger

from .config import build_locators, load_configuration
from .models import CheckResult, LocatorSet, Luxmed, describe_failure
from .notifications import Notifier, build_notifiers, dispatch
from .portal import LuxMedPortal
from .state import NotificationStore

# Matches the health check's 3x window, so the CRITICAL line and the unhealthy flag appear
# together rather than hours apart.
FAILED_CYCLES_BEFORE_CRITICAL = 3


def write_heartbeat(delay: int) -> None:
    """Records how long this cycle vouches for the process, for an external health check.

    Opt-in through LUXMED_HEARTBEAT_FILE, so nothing appears outside a container. The file holds
    a deadline rather than a timestamp: the checker then needs no knowledge of the interval.
    """
    path = os.environ.get("LUXMED_HEARTBEAT_FILE")
    if path:
        Path(path).write_text(str(int(time.time()) + delay * 3), encoding="utf-8")


def run_checks(
    portal: LuxMedPortal,
    locator_set: LocatorSet,
    store: NotificationStore,
    notifiers: list[Notifier],
    lookup_days: int,
) -> CheckResult:
    """Runs every enabled search, reporting how many ran and how many reached the portal.

    Both counts drive the heartbeat: per-locator failures are swallowed below so one broken
    search does not stop the others, which means "this returned" says nothing about whether
    anything worked.

    A locator that would not even parse counts as enabled-but-not-searched, exactly like one
    whose search failed. Otherwise a config in which every locator is malformed would report
    `enabled == 0` and be taken for a config with every search switched off on purpose.
    """
    enabled = len(locator_set.errors)
    searched = 0
    last_error = locator_set.errors[-1] if locator_set.errors else ""
    for locator in locator_set.locators:
        if not locator.enabled:
            logger.info(f"Appointments skipping: {locator.name}, disabled")
            continue

        enabled += 1
        try:
            appointments = portal.search_appointments(locator, lookup_days)
            searched += 1
            if not appointments:
                logger.info(f"No appointments found for: {locator.name}")
                continue
            for appointment in appointments:
                logger.info(
                    "Appointment found for: {app_name}! {AppointmentDate} "
                    "at {ClinicPublicName} - {DoctorName}".format(
                        **appointment.__dict__,
                        app_name=locator.name,
                    ),
                )
                if store.is_known(appointment):
                    logger.info(f"Notification was already sent for: {locator.name}")
                    continue
                store.remember(appointment)
                dispatch(notifiers, locator, appointment)
                logger.info(
                    "Notification sent for: {app_name}! {AppointmentDate} "
                    "at {ClinicPublicName} - {DoctorName}".format(
                        **appointment.__dict__,
                        app_name=locator.name,
                    ),
                )
        except Exception as e:
            logger.exception(f"Looking for appointments for {locator} failed, reason: {e}")
            last_error = describe_failure(e)

    return CheckResult(enabled=enabled, searched=searched, last_error=last_error)


class CycleRunner:
    """Owns the one piece of state that outlives a cycle: how many have failed in a row.

    It has to outlive a cycle - the escalation to CRITICAL fires on the third consecutive
    failure, so a fresh instance per cycle would never get past one.
    """

    def __init__(self, config_paths: list[str], delay: int = 0) -> None:
        self.config_paths = config_paths
        self.delay = delay
        self.failed_cycles = 0

    def _collaborators(self) -> tuple[LuxMedPortal, LocatorSet, NotificationStore, list[Notifier], int]:
        """Everything is rebuilt from the config file each cycle, which is what makes an edit
        apply without a restart. Logging in once per cycle is unavoidable anyway: the portal's
        token lives 10 minutes.

        Order matters: everything that can fail on the config alone is built first, so a bad
        provider section costs nothing. Logging in first would spend a full credentialed request
        on every cycle of a config that was never going to work - against an account LUX MED is
        known to lock for hammering it.
        """
        config: dict[str, Any] = load_configuration(self.config_paths)
        luxmed = Luxmed(**config["luxmed"])
        locator_set = build_locators(config)
        store = NotificationStore(config["misc"]["notifydb"])
        notifiers = build_notifiers(config)

        portal = LuxMedPortal(luxmed.login, luxmed.password)
        portal.login()

        return (
            portal,
            locator_set,
            store,
            notifiers,
            config["luxmedsniper"]["lookup_time_days"],
        )

    def run_cycle(self) -> None:
        try:
            result = run_checks(*self._collaborators())
        except Exception as e:  # schedule re-raises out of run_pending(), ending the process
            logger.exception("Cycle failed, retrying on the next run")
            # enabled=1: login died before any locator ran, which is an outage, not an idle config.
            result = CheckResult(enabled=1, searched=0, last_error=describe_failure(e))

        if not result.enabled:
            # Every search is disabled on purpose. Nothing ran, but nothing is broken either, so
            # this must not raise an alert claiming the account is locked.
            logger.info("No locator is enabled, nothing to search")
            self.failed_cycles = 0
            write_heartbeat(self.delay)
            return

        if result.searched:
            self._announce_recovery()
            self.failed_cycles = 0
            write_heartbeat(self.delay)
            return

        self._announce_failure(result)

    def _announce_recovery(self) -> None:
        if self.failed_cycles >= FAILED_CYCLES_BEFORE_CRITICAL:
            # Only worth a push if the outage was announced; otherwise "recovered" would refer
            # to something nobody was told about.
            logger.bind(
                alert=True,
                pushover=(
                    f"Komunikacja z LuxMedem przywrócona – wyszukiwanie działa normalnie "
                    f"po {self.failed_cycles} nieudanych cyklach "
                    f"(~{self.failed_cycles * self.delay // 60} min)."
                ),
            ).success(
                "Talking to LuxMed again - searches are running normally after {} failed "
                "cycles (~{} min).",
                self.failed_cycles,
                self.failed_cycles * self.delay // 60,
            )
        elif self.failed_cycles:
            logger.success("Recovered after {} failed cycle(s)", self.failed_cycles)

    def _announce_failure(self, result: CheckResult) -> None:
        self.failed_cycles += 1
        if self.failed_cycles < FAILED_CYCLES_BEFORE_CRITICAL:
            logger.warning("No search reached the portal this cycle ({}. in a row)", self.failed_cycles)
        elif self.failed_cycles == FAILED_CYCLES_BEFORE_CRITICAL:
            # Exactly once per outage. CRITICAL is wired to Pushover, and an alert per cycle
            # would be a flood rather than a signal - the health check already reports the
            # ongoing state.
            minutes = (self.failed_cycles - 1) * self.delay // 60  # first failure is a cycle back
            reason = result.last_error or "unknown"
            logger.bind(
                pushover=(
                    f"Brak kontaktu z portalem LuxMed od ~{minutes} min "
                    f"(nieudane cykle: {self.failed_cycles}). Ostatni błąd: {reason}. "
                    "Sprawdź dane logowania, czy konto nie zostało zablokowane i czy API portalu "
                    "odpowiada. Wysyłane raz na awarię – kolejną wiadomość dostaniesz, gdy "
                    "wyszukiwanie znów zadziała."
                ),
            ).critical(
                "No search has reached the portal for ~{} min ({} cycles in a row). Last failure: "
                "{}. Check the credentials, whether the account is locked, and the portal API. "
                "Sent once per outage - you will get another message once searches work again.",
                minutes,
                self.failed_cycles,
                reason,
            )
        else:
            logger.warning("Still not reaching the portal ({}. cycle in a row)", self.failed_cycles)
