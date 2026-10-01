import base64
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.handoff import request_handoff
from app.real_appointments import GoogleCalendarAppointmentService
from app.store import Store
from app.main import app
from app.appointments import BookingError
from app.security import mint_token
from test_voice import client


def test_email_has_calendar_attachment_with_same_uid_and_time():
    service = object.__new__(GoogleCalendarAppointmentService)
    service.timezone = ZoneInfo('Asia/Dubai')
    service.host_google = SimpleNamespace(access_token=lambda _: ('token', {}))
    sent = {}
    def send(token, method, url, **kwargs):
        sent.update(kwargs['json'])
        return SimpleNamespace(json=lambda: {'id': 'sent-1'})
    service._google_request = send
    appointment = {'start': datetime(2026, 10, 5, 9, tzinfo=timezone.utc),
                   'end': datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc),
                   'reason': 'Cleaning, follow-up\nBring records',
                   'calendar_ical_uid': 'existing-event@google.com', 'calendar_sequence': 2}
    assert service._send_confirmation_email({'id': 'primary', 'name': 'Private owner'},
        {'google_email': 'mukuloli43@gmail.com'}, {'name': 'Patient', 'email': 'patient@example.com'}, appointment) == 'sent-1'
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(sent['raw']))
    assert message['From'] == 'mukuloli43@gmail.com'
    assert message['To'] == 'patient@example.com'
    assert 'Private owner' not in message.as_string()
    attachment, = message.iter_attachments()
    assert attachment.get_filename() == 'appointment.ics'
    assert attachment.get_content_type() == 'text/calendar'
    calendar = attachment.get_content()
    assert 'UID:existing-event@google.com' in calendar
    assert 'DTSTART:20261005T090000Z' in calendar
    assert 'SEQUENCE:2' in calendar
    assert 'DESCRIPTION:Cleaning\\, follow-up\\nBring records' in calendar


def test_handoff_persists_and_deduplicates(tmp_path):
    store = Store(tmp_path / 'followups.db')
    service = object()
    for _ in range(2):
        response = request_handoff(store, service, 'patient', 'session', 'CALENDAR_UNAVAILABLE')
        assert response['handoff_requested'] is True
        assert response['ok'] is False
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM human_followups').fetchone()[0] == 1


def test_handoff_does_not_promise_contact_if_storage_fails():
    def fail(_):
        raise RuntimeError('offline')
    response = request_handoff(SimpleNamespace(save_followup=fail), object(), 'p', 's', 'UNAVAILABLE')
    assert response['handoff_requested'] is False
    assert 'contact reception' in response['message']


def test_failed_booking_tool_creates_human_followup(client, monkeypatch):
    app.state.store.create_session('session', 'patient')
    token = mint_token({'scope': 'agent', 'uid': 'patient', 'session_id': 'session'})
    def fail(*args):
        raise BookingError('GOOGLE_API_UNAVAILABLE', 'technical detail')
    monkeypatch.setattr(app.state.service, 'availability', fail)
    response = client.post('/internal/voice/session/tools', headers={'Authorization': 'Bearer ' + token},
                           json={'name': 'check_availability', 'arguments': {'doctor_id': 'dr-sara', 'date': '2026-10-05'}})
    assert response.status_code == 200
    assert response.json()['handoff_requested'] is True
    assert 'technical detail' not in response.json()['message']
    assert app.state.service.appointments('patient')['appointments'] == []
