"""Talking to the LUX MED Patient Portal: the session, the search, and the id dictionaries."""

import datetime
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import requests
from loguru import logger

from .models import Appointment, DoctorLocator, LuxmedSniperError

REQUEST_TIMEOUT = 30
LOG_BODY_MAX_CHARS = 500

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


def parse_appointments(payload: dict[str, Any], locator: DoctorLocator) -> list[Appointment]:
    """Turns a terms/index payload into appointments, keeping only the doctors and clinics the
    search asked for. Takes the parsed JSON rather than a response, so it can be tested on its
    own."""
    appointments = []
    for term_for_day in payload["termsForService"]["termsForDays"]:
        for term in term_for_day["terms"]:
            doctor = term["doctor"]
            clinic_id = int(term["clinicGroupId"])
            doctor_id = int(doctor["id"])

            if locator.doctor_ids and doctor_id not in locator.doctor_ids:
                continue
            if locator.clinic_ids and clinic_id not in locator.clinic_ids:
                continue

            appointments.append(
                Appointment(
                    datetime.datetime.fromisoformat(term["dateTimeFrom"]).replace(
                        tzinfo=ZoneInfo("Europe/Warsaw"),
                    ),
                    term["clinicId"],
                    term["clinic"],
                    f"{doctor['academicTitle']} {doctor['firstName']} {doctor['lastName']}",
                    term["serviceId"],
                ),
            )
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
        return response

    @staticmethod
    def _is_session_expired(response: requests.Response) -> bool:
        """With X-Requested-With (see BROWSER_HEADERS) a rejected session yields a plain 401,
        which is the main path here. The non-JSON check stays as a fallback: without that
        header the portal answers 200 with a login page, so detection keeps working should
        the portal stop honouring it. Trade-off: any non-JSON reply (a 500 error page, say)
        is reported as a rejected session too.
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
        if LuxMedPortal._is_session_expired(response):
            err_msg = f"Session rejected by the portal ({response.status_code})"
            raise LuxmedSniperError(err_msg, response.status_code)
        return response

    def search_appointments(self, locator: DoctorLocator, lookup_days: int) -> list[Appointment]:
        logger.info(f"Get appointments for: {locator}")

        date_to = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=lookup_days)

        params = {
            "searchPlace.id": locator.city_id,
            "searchPlace.type": 0,
            "serviceVariantId": locator.service_id.split(",")[0],
            "languageId": 10,
            "searchDateFrom": datetime.datetime.now(tz=datetime.UTC).date().strftime("%Y-%m-%d"),
            "searchDateTo": date_to.strftime("%Y-%m-%d"),
            "searchDatePreset": lookup_days,
            "delocalized": "false",
            "processId": str(uuid.uuid4()),
        }

        if locator.clinic_ids:
            params["facilitiesIds"] = locator.clinic_ids
        if locator.doctor_ids:
            params["doctorsIds"] = locator.doctor_ids

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
