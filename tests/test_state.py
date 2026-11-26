"""The already-notified store."""

import datetime
import json

from luxmedsniper.models import Appointment
from luxmedsniper.state import NotificationStore


def appointment(doctor="lek. Jan Kowalski", when="2026-10-09T14:30:00"):
    return Appointment(
        datetime.datetime.fromisoformat(when),
        clinic_id=1,
        ClinicPublicName="ul. Testowa 1",
        DoctorName=doctor,
        service_id=4502,
    )


def test_an_unseen_appointment_is_not_known(tmp_path):
    store = NotificationStore(tmp_path / "n.db")

    assert store.is_known(appointment()) is False


def test_a_remembered_appointment_is_known(tmp_path):
    store = NotificationStore(tmp_path / "n.db")
    store.remember(appointment())

    assert store.is_known(appointment()) is True


def test_remembering_survives_a_new_store_on_the_same_file(tmp_path):
    path = tmp_path / "n.db"
    NotificationStore(path).remember(appointment())

    assert NotificationStore(path).is_known(appointment()) is True


def test_a_different_time_for_the_same_doctor_is_a_different_appointment(tmp_path):
    store = NotificationStore(tmp_path / "n.db")
    store.remember(appointment(when="2026-10-09T14:30:00"))

    assert store.is_known(appointment(when="2026-10-09T15:00:00")) is False


def test_each_doctor_keeps_their_own_list(tmp_path):
    store = NotificationStore(tmp_path / "n.db")
    store.remember(appointment(doctor="A"))

    assert store.is_known(appointment(doctor="B")) is False


def test_remember_writes_through_immediately(tmp_path):
    """Each remember hits the disk, so a crash mid-cycle cannot re-announce what was already
    sent."""
    path = tmp_path / "n.db"
    store = NotificationStore(path)
    store.remember(appointment())

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "lek. Jan Kowalski": ["2026-10-09T14:30:00"],
    }


def test_a_corrupted_file_is_treated_as_empty_and_logged_once(tmp_path, log):
    path = tmp_path / "n.db"
    path.write_text("{not json", encoding="utf-8")

    store = NotificationStore(path)

    assert store.is_known(appointment()) is False
    assert log.getvalue().count("Database file corrupted") == 1


def test_a_corrupted_file_is_overwritten_rather_than_appended_to(tmp_path):
    path = tmp_path / "n.db"
    path.write_text("{not json", encoding="utf-8")

    NotificationStore(path).remember(appointment())

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "lek. Jan Kowalski": ["2026-10-09T14:30:00"],
    }
