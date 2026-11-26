"""Command line entry point: argument parsing, the --dump-ids tool, and the scheduler loop."""

import argparse
import copy
import json
import os
import re
import time
import unicodedata
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import schedule
from loguru import logger

from .alerts import add_critical_alert_sink, setup_logging
from .config import load_configuration
from .models import Luxmed
from .portal import LuxMedPortal
from .runner import CycleRunner

IDS_DIR = Path("luxmed-ids")
# NFKD strips the accents off ą ć ę ó ś ź ż, but ł has no decomposition and would be dropped.
UNDECOMPOSABLE = str.maketrans({"ł": "l", "Ł": "L"})


def facilities_filename(city_wildcard: str | None) -> str:
    """`ids-facilities-doctors.json`, or a `-<city>` variant when the dump was narrowed.

    Named after the pattern that was passed rather than the cities it matched: a wildcard can
    match several, and the filename should be predictable before the run rather than after it.
    """
    base = "ids-facilities-doctors"
    if city_wildcard is None:
        return f"{base}.json"

    ascii_name = (
        unicodedata.normalize("NFKD", city_wildcard.translate(UNDECOMPOSABLE))
        .encode("ascii", "ignore")
        .decode()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    # A pattern of nothing but wildcards slugifies to "", and `ids-facilities-doctors-.json`
    # helps nobody.
    return f"{base}-{slug}.json" if slug else f"{base}.json"


def flatten_services(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The portal returns services as a group / subgroup / variant tree; the dump is a flat list.

    Deliberately exactly three levels, matching what the portal sends and what the previous
    hand-rolled loops did. Recursing to arbitrary depth would silently produce a differently
    shaped file the day the portal adds a level.
    """
    services: list[dict[str, Any]] = []
    for group in raw:
        services.append(
            dict(id=group['id'], name=group['name'], telemedicine=group['isTelemedicine']),
        )
        for subgroup in group['children']:
            services.append(
                dict(id=subgroup['id'], name=subgroup['name'],
                     telemedicine=subgroup['isTelemedicine']),
            )
            services.extend(
                dict(id=variant['id'], name=variant['name'],
                     telemedicine=variant['isTelemedicine'])
                for variant in subgroup['children']
            )
    return services


# noinspection PyTypeChecker
def dump_current_ids(config, city_wildcard: str | None, dump_ids_doctors: bool) -> None:
    configuration = load_configuration(config)
    luxmed = Luxmed(**configuration["luxmed"])
    portal = LuxMedPortal(luxmed.login, luxmed.password)
    # Explicit: the client no longer logs in from its constructor.
    portal.login()

    cities: list[dict[str, Any]] = portal.get_cities()
    logger.info(f'Found: {len(cities)} cities')
    services = flatten_services(portal.get_services())
    logger.info(f'Found: {len(services)} services')
    facilities_and_doctors = {}  # per city, per service
    for city in cities:
        if city_wildcard is not None and not fnmatch(city['name'], city_wildcard):
            logger.info(f'{city["name"]} - skipping facilities and doctors')
            continue
        if dump_ids_doctors is False:
            continue
        logger.info(f'{city["name"]} - looking for facilities and doctors')
        facilities_and_doctors[city['id']] = {}
        for service in services:
            facilities_and_doctors[city['id']][service['id']] = {}
            fac_and_doc: dict[str, Any] = portal.get_facilities_and_doctors(city['id'], service['id'])
            facilities_and_doctors[city['id']][service['id']]['facilities'] = copy.deepcopy(fac_and_doc['facilities'])
            facilities_and_doctors[city['id']][service['id']]['doctors'] = [
                dict(id=d['id'], name=f'{d["academicTitle"]} {d["lastName"]} {d["firstName"]}')
                for d in fac_and_doc['doctors']
            ]

    dumps = [("ids-cities.json", cities), ("ids-services.json", services)]
    if dump_ids_doctors:
        dumps.append((facilities_filename(city_wildcard), facilities_and_doctors))
    else:
        # Writing it anyway would replace a previous dump with {} - and that dump costs hundreds
        # of requests against an account LUX MED locks for hammering it.
        logger.info(
            "Facilities and doctors not fetched (needs --dump-ids-doctors); "
            "leaving any existing dump untouched",
        )

    for filename, payload in dumps:
        with (IDS_DIR / filename).open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)
        logger.info("Wrote {}", IDS_DIR / filename)

    if dump_ids_doctors:
        logger.warning(
            "{} holds doctors' names - personal data. It is gitignored on purpose; keep it local.",
            IDS_DIR / facilities_filename(city_wildcard),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "-c", "--config",
        help="Configuration file path", default=["luxmed_sniper.yaml"],
        nargs="*"
    )
    parser.add_argument(
        "-d", "--delay",
        type=int, help="Delay in fetching updates [s]",
        # Env var so a container can set it without a shell wrapper: an exec-form CMD expands
        # nothing, and shell form would leave /bin/sh as PID 1 swallowing SIGTERM. `or` rather
        # than a get() default because compose passes an unset variable through as "".
        default=int(os.environ.get("LUXMED_INTERVAL") or 1800),
    )
    group = parser.add_argument_group('dump-ids')
    group.add_argument(
        "--dump-ids",
        action='store_true', dest='dump_ids', help="Dump current ids", default=False
    )
    group.add_argument(
        "--dump-ids-city",
        type=str, dest='dump_ids_city', help="Dump facilities and doctors only from this city (wildcard)", default=None
    )
    group.add_argument(
        "--dump-ids-doctors",
        action='store_true', dest='dump_ids_doctors', default=False,
        help="Dump facilities and doctors also (many requests)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    logger.info("Lux Med Appointment Sniper")
    args = build_parser().parse_args(argv)

    # `-c` with no values leaves an empty list, which would otherwise sail past the loop below,
    # load an empty config, and leave the scheduler looping forever on a cycle that can never
    # work - a KeyError per interval and a CRITICAL alert after three.
    if not args.config:
        logger.error("No configuration file given")
        return 1

    # Fail fast on a config that is not a readable file. A missing bind mount source makes Docker
    # create a directory in its place, and without this the process would log IsADirectoryError
    # every cycle forever while still reporting itself as running.
    for configuration_file in args.config:
        if not Path(configuration_file).expanduser().is_file():
            logger.error("Not a readable configuration file: {}", configuration_file)
            return 1

    if args.dump_ids is True:
        logger.info('Dumping IDs')
        dump_current_ids(args.config, args.dump_ids_city, args.dump_ids_doctors)
    else:
        # Loaded once here purely to wire up alerting; the runner reloads it every cycle, which
        # is what keeps config edits live. A config that cannot be parsed stops us right here.
        add_critical_alert_sink(load_configuration(args.config))

        logger.info(f'Start working every: {args.delay} s')
        # One runner for the life of the process: the consecutive-failure count it carries is
        # what the escalation to CRITICAL counts, so it must survive between cycles.
        runner = CycleRunner(args.config, args.delay)
        runner.run_cycle()
        # A scheduler of our own rather than the module-global default, so nothing leaks between
        # runs within one process.
        scheduler = schedule.Scheduler()
        scheduler.every(args.delay).seconds.do(runner.run_cycle)
        try:
            while True:
                scheduler.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            ...
    logger.info('Exiting')
    return 0
