"""Which appointments have already been announced.

A JSON file mapping a doctor's name to the ISO timestamps already notified about. The format,
the key and the write-per-appointment are all as they were when this lived on the client class -
only the two functions that re-read the file from disk on every single lookup have become one
object that loads once.
"""

import datetime
import json
from pathlib import Path

from loguru import logger

from .models import Appointment


class NotificationStore:
    """Load once, write on every change.

    Writing on each `remember` rather than once per cycle is deliberate: it keeps the crash
    semantics the two original functions had, where an appointment already announced stays
    recorded even if the process dies halfway through the cycle.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._notified: dict[str, list[str]] = self._load()

    def _load(self) -> dict[str, list[str]]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open(encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.exception("Database file corrupted, treating as empty")
            return {}

    def is_known(self, appointment: Appointment) -> bool:
        seen = map(datetime.datetime.fromisoformat, self._notified.get(appointment.DoctorName, []))
        return appointment.AppointmentDate in seen

    def remember(self, appointment: Appointment) -> None:
        """Writes first, and only then treats the appointment as announced.

        The other way round, a failed write (full disk, read-only volume) would still leave the
        slot marked as known in memory, so a second locator finding the same slot in this cycle
        would skip it - and nothing would be on disk to make the next cycle retry.
        """
        seen = self._notified.get(appointment.DoctorName, [])
        updated = {
            **self._notified,
            appointment.DoctorName: [*seen, appointment.AppointmentDate.isoformat()],
        }
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(updated, f, indent=2, ensure_ascii=False)
        self._notified = updated
