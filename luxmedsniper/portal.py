"""Talking to the LUX MED Patient Portal: the session, the search, and the id dictionaries."""

import datetime
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import requests
from loguru import logger
from pydantic import BaseModel, ValidationError

from .models import Appointment, DoctorLocator, LuxmedSniperError, PortalMaintenance

REQUEST_TIMEOUT = 30
LOG_BODY_MAX_CHARS = 500
MAINTENANCE_MARKER = "Przerwa techniczna"  # <title> of the static page served during planned work

BROWSER_HEADERS = {  # what the Patient Portal SPA sends
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pl-PL,pl;q=0.9",
    # Without this an expired session answers 200 with a 12 kB login page instead of 401.
    "X-Requested-With": "XMLHttpRequest",
}


class _Doctor(BaseModel):
    id: int
    academicTitle: str | None = None
    firstName: str | None = None
    lastName: str | None = None


class _Term(BaseModel):
    """One slot as the portal sends it.

    Only what the search cannot work without is required: the time, and the two ids the locator
    filters on. Everything else merely decorates the notification, and a slot announced with a
    blank clinic name beats a slot dropped for the lack of one.
    """

    dateTimeFrom: datetime.datetime
    clinicGroupId: int
    doctor: _Doctor
    clinicId: int | None = None
    clinic: str = ""
    serviceId: int | None = None


def _where(error: ValidationError) -> str:
    """Names the offending field and what is wrong with it - never the value, which for a term
    is a doctor's name and for anything else is whatever the portal chose to send."""
    first = error.errors(include_input=False, include_url=False)[0]
    return f"{'.'.join(map(str, first['loc']))}: {first['msg']}"


def _raw_terms(payload: Any) -> list[Any]:
    """Walks the envelope down to the flat list of slots, refusing anything shaped differently.

    A reply without the envelope is not an empty schedule. Read leniently it would be taken for
    one, and the search would report "nothing free" for as long as the portal kept sending it.
    """
    try:
        days = payload["termsForService"]["termsForDays"]
        if not isinstance(days, list):
            raise TypeError
        raw_terms = []
        for day in days:
            if not isinstance(day["terms"], list):
                raise TypeError
            raw_terms.extend(day["terms"])
    except (KeyError, TypeError) as e:
        err_msg = "Search response has an unexpected shape: no termsForService.termsForDays[].terms"
        raise LuxmedSniperError(err_msg) from e
    return raw_terms


def parse_appointments(payload: Any, locator: DoctorLocator) -> list[Appointment]:
    """Turns a terms/index payload into appointments, keeping only the doctors and clinics the
    search asked for. Takes the parsed JSON rather than a response, so it can be tested on its
    own.

    Each slot is validated on its own: one the portal sent malformed is logged and skipped, and
    the rest are still announced. Only when not a single slot of a non-empty reply can be read
    is the whole search failed, since that is the portal changing its format rather than one odd
    record - and a failed search is what reaches the outage alert.
    """
    raw_terms = _raw_terms(payload)
    warsaw = ZoneInfo("Europe/Warsaw")

    appointments = []
    malformed = ""
    for raw_term in raw_terms:
        try:
            term = _Term.model_validate(raw_term)
        except ValidationError as e:
            malformed = _where(e)
            logger.error("Skipping a malformed slot for {}: {}", locator.name, malformed)
            continue

        doctor = term.doctor
        if locator.doctor_ids and doctor.id not in locator.doctor_ids:
            continue
        if locator.clinic_ids and term.clinicGroupId not in locator.clinic_ids:
            continue

        starts = term.dateTimeFrom
        appointments.append(
            Appointment(
                # The portal sends local wall-clock time without an offset; honour one if it appears.
                starts.astimezone(warsaw) if starts.tzinfo else starts.replace(tzinfo=warsaw),
                term.clinicId,
                term.clinic,
                " ".join(filter(None, (doctor.academicTitle, doctor.firstName, doctor.lastName))),
                term.serviceId,
            ),
        )

    if malformed and not appointments:
        err_msg = f"Search response has an unexpected shape: {malformed}"
        raise LuxmedSniperError(err_msg)
    return appointments


class LuxMedPortal:
    LUXMED_LOGIN_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/Account/LogIn'
    NEW_PORTAL_RESERVATION_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/terms/index'

    DICTIONARY_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/Dictionary'
    DICTIONARY_CITIES_URL = f'{DICTIONARY_URL}/cities'
    DICTIONARY_SERVICES_URL = f'{DICTIONARY_URL}/serviceVariantsGroups'
    DICTIONARY_FACILITIES_AND_DOCTORS = f'{DICTIONARY_URL}/facilitiesAndDoctors'

    def __init__(self, login: str, password: str, session: requests.Session | None = None) -> None:
        """`session` exists for tests: pass a stub and nothing has to patch requests globally.
        Production passes nothing and `login()` builds its own."""
        self._login = login
        self._password = password
        self._given_session = session
        self.session = session

    def login(self) -> None:
        # A fresh session each time: login() runs again whenever the token lapses, and the
        # portal's own cookies are the only thing authenticating us. A session handed in by a
        # test is kept, otherwise the stub would be thrown away here.
        self.session = self._given_session or requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

        response = self._request(
            "POST",
            LuxMedPortal.LUXMED_LOGIN_URL,
            json={"login": self._login, "password": self._password},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code != requests.codes["ok"]:
            err_msg = f"Unexpected response {response.status_code}, cannot log in"
            raise LuxmedSniperError(err_msg, response.status_code)

        logger.info("Successfully logged in!")

        # Cookies alone authenticate; the portal returns the token as the Authorization-Token
        # cookie, so resending it as a header (as this script used to) is ignored by the server.
        self.session.cookies = response.cookies

    @staticmethod
    def _log_http(response: requests.Response, *, redact_body: bool) -> None:
        """One INFO line per request.

        Never logs request bodies (POST /Account/LogIn carries the password) nor the login
        response body (it carries the session token). Of the response headers only
        Content-Type is logged, since Set-Cookie carries the session cookies.
        """
        if redact_body:
            body = f"<skipped: {len(response.content)} B>"
        else:
            text = response.text
            body = text[:LOG_BODY_MAX_CHARS].replace("\n", " ").replace("\r", " ")
            if len(text) > LOG_BODY_MAX_CHARS:
                body += f"... (+{len(text) - LOG_BODY_MAX_CHARS} chars)"

        logger.info(
            "HTTP {} {} -> {} | {} | {}",
            response.request.method,
            response.request.url or "",  # post-redirect URL, and the one carrying the query string
            response.status_code,
            response.headers.get("Content-Type", "-"),
            body,
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        # Redact on the URL we asked for, never on response.request.url: the latter is the
        # post-redirect one, so a 302 on the login endpoint would unmask the session token.
        # Only a successful login carries a token, so a failed one stays readable - that body is
        # what tells a maintenance window apart from a WAF block.
        redact = url.startswith(LuxMedPortal.LUXMED_LOGIN_URL) and response.status_code == requests.codes["ok"]
        LuxMedPortal._log_http(response, redact_body=redact)
        if LuxMedPortal._is_maintenance(response):
            err_msg = f"The portal is down for maintenance ({response.status_code})"
            raise PortalMaintenance(err_msg, response.status_code)
        return response

    @staticmethod
    def _is_maintenance(response: requests.Response) -> bool:
        """During planned work every endpoint, login and JSON API alike, answers 503 with the
        same static page. Checked here so it is recognised whichever request meets it first.
        A 503 without the marker stays an ordinary failure and escalates as one.
        """
        return (
            response.status_code == requests.codes["service_unavailable"]
            and MAINTENANCE_MARKER in response.text
        )

    @staticmethod
    def _is_session_expired(response: requests.Response) -> bool:
        """With X-Requested-With (see BROWSER_HEADERS) a rejected session yields a plain 401,
        which is the main path here. The non-JSON check stays as a fallback: without that
        header the portal answers 200 with a login page, so detection keeps working should
        the portal stop honouring it. Trade-off: any non-JSON 200 is reported as a rejected
        session too. Error statuses never get this far - `_get` turns them away first.
        """
        if response.status_code in (requests.codes["unauthorized"], requests.codes["forbidden"]):
            return True
        try:
            response.json()
        except ValueError:
            return True
        return False

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        """GET that fails loudly when the portal rejected the session, instead of letting a
        login page reach the JSON parser. One login per cycle means the 10-minute token cannot
        realistically lapse mid-cycle, so there is no retry here - the next cycle logs in anew."""
        response = self._request("GET", url, **kwargs)
        if response.status_code in (requests.codes["unauthorized"], requests.codes["forbidden"]):
            err_msg = f"Session rejected by the portal ({response.status_code})"
            raise LuxmedSniperError(err_msg, response.status_code)
        if response.status_code != requests.codes["ok"]:
            # Checked before the body: an error can arrive as perfectly good JSON, and parsing
            # that would report a missing key instead of the status that explains it.
            err_msg = f"The portal answered with an error ({response.status_code})"
            raise LuxmedSniperError(err_msg, response.status_code)
        if LuxMedPortal._is_session_expired(response):
            err_msg = f"Session rejected by the portal ({response.status_code})"
            raise LuxmedSniperError(err_msg, response.status_code)
        return response

    def search_appointments(self, locator: DoctorLocator, lookup_days: int) -> list[Appointment]:
        logger.info(f"Get appointments for: {locator}")

        now = datetime.datetime.now().astimezone()  # system zone: the portal's "today" is the patient's
        date_to = now + datetime.timedelta(days=lookup_days)

        params = {
            "searchPlace.id": locator.city_id,
            "searchPlace.type": 0,
            "serviceVariantId": locator.service_id.split(",")[0],
            "languageId": 10,
            "searchDateFrom": now.strftime("%Y-%m-%d"),
            "searchDateTo": date_to.strftime("%Y-%m-%d"),
            "searchDatePreset": lookup_days,
            "delocalized": "false",
            "processId": str(uuid.uuid4()),
        }

        if locator.clinic_ids:
            params["facilitiesIds"] = locator.clinic_ids
        if locator.doctor_ids:
            params["doctorsIds"] = locator.doctor_ids
        if locator.referral_id is not None:
            params["referralId"] = locator.referral_id
            params["referralTypeId"] = locator.referral_type_id

        logger.debug("Parameters: {}", params)

        response = self._get(LuxMedPortal.NEW_PORTAL_RESERVATION_URL, params=params)
        appointments = [
            appointment
            for appointment in parse_appointments(response.json(), locator)
            if appointment.AppointmentDate <= date_to
        ]
        logger.info("Found {} appointments for: {}", len(appointments), locator.name)
        return appointments

    def get_cities(self) -> list[dict]:
        response = self._get(
            LuxMedPortal.DICTIONARY_CITIES_URL,
            headers={"Content-Type": "application/json"},
        )
        return response.json()

    def get_services(self) -> list[dict]:
        response = self._get(
            LuxMedPortal.DICTIONARY_SERVICES_URL,
            headers={"Content-Type": "application/json"},
        )
        return response.json()

    def get_facilities_and_doctors(self, city_id: int, service_variant_id: int) -> dict[str, Any]:
        params = {
            'cityId': city_id,
            'serviceVariantId': service_variant_id
        }
        response = self._get(
            LuxMedPortal.DICTIONARY_FACILITIES_AND_DOCTORS,
            params=params,
            headers={"Content-Type": "application/json"},
        )
        return response.json()
