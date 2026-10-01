from datetime import datetime, timezone

import pytest

from app.appointments import BookingError
from app.config import settings
from app.host_google import GOOGLE_SCOPES, HostGoogleOAuth
from app.real_appointments import GoogleCalendarAppointmentService
from app.security import Patient


def test_host_oauth_requests_calendar_and_email_permissions():
    assert 'https://www.googleapis.com/auth/calendar.events' in GOOGLE_SCOPES
    assert 'https://www.googleapis.com/auth/gmail.send' in GOOGLE_SCOPES


def test_only_allowlisted_host_can_manage_calendar(monkeypatch):
    oauth = object.__new__(HostGoogleOAuth)
    monkeypatch.setattr(settings, 'host_admin_emails', 'owner@example.com')
    oauth.require_host_admin(Patient('host', 'Owner', 'owner@example.com'))
    with pytest.raises(BookingError, match='not authorized'):
        oauth.require_host_admin(Patient('customer', 'Customer', 'customer@example.com'))


def test_calendar_event_invites_customer(monkeypatch):
    service = object.__new__(GoogleCalendarAppointmentService)
    service.timezone = timezone.utc
    monkeypatch.setattr(settings, 'google_meet_enabled', False)
    doctor = {'id': 'doctor-1', 'name': 'Dr Test', 'location': 'Clinic'}
    customer = {'id': 'customer-1', 'name': 'Alex', 'email': 'alex@example.com'}
    hold = {'reason': 'checkup', 'start': datetime(2026, 10, 2, 9, tzinfo=timezone.utc),
            'end': datetime(2026, 10, 2, 9, 30, tzinfo=timezone.utc)}
    payload = service._event_payload('appointment-1', doctor, hold, customer, include_id=True)
    assert payload['attendees'] == [{'email': 'alex@example.com', 'displayName': 'Alex'}]
    assert payload['extendedProperties']['private']['hostId'] == 'doctor-1'
    assert payload['id'].startswith('noor')
