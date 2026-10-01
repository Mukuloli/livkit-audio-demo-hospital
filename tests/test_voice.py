from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.appointments import AppointmentService, BookingError, DUBAI
from app.config import settings
from app.dialog import VoiceDialog, resolve_day
from app.main import app
from app.security import mint_token
from app.store import Store


def working_day(offset=1):
    day = datetime.now(DUBAI).date() + timedelta(days=offset)
    while day.weekday() == 6:
        day += timedelta(days=1)
    return day.isoformat()


@pytest.fixture
def service(tmp_path):
    return AppointmentService(Store(tmp_path / 'test.db'))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'database_path', str(tmp_path / 'api.db'))
    monkeypatch.setattr(settings, 'app_signing_secret', 'test-signing-secret-with-at-least-32-characters')
    monkeypatch.setattr(settings, 'auth_mode', 'demo')
    monkeypatch.setattr(settings, 'booking_mode', 'local_demo')
    monkeypatch.setattr(settings, 'app_env', 'development')
    with TestClient(app) as client:
        yield client


def login(client):
    response = client.post('/auth/demo', json={'name': 'Test Patient'})
    assert response.status_code == 200
    return {'Authorization': 'Bearer ' + response.json()['token']}


def test_same_slot_concurrent_hold_has_one_winner(service):
    slot = service.availability('dr-sara', working_day())['slots'][0]

    def attempt(i):
        try:
            return service.prepare(f'patient-{i}', f'session-{i}', 'dr-sara', slot['start'], 'cleaning')['hold_id']
        except BookingError as exc:
            assert exc.code == 'SLOT_UNAVAILABLE'
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        holds = list(pool.map(attempt, range(8)))
    assert sum(h is not None for h in holds) == 1


def test_confirmation_is_idempotent_and_owner_bound(service):
    slot = service.availability('dr-sara', working_day())['slots'][0]
    hold = service.prepare('p1', 's1', 'dr-sara', slot['start'], 'cleaning')
    with pytest.raises(BookingError) as error:
        service.confirm('p2', 's1', hold['hold_id'])
    assert error.value.code == 'HOLD_EXPIRED'
    first = service.confirm('p1', 's1', hold['hold_id'])
    assert service.confirm('p1', 's1', hold['hold_id']) == first
    assert len(service.appointments('p1')['appointments']) == 1
    assert service.appointments('p2')['appointments'] == []


def test_expired_hold_cannot_confirm(service):
    slot = service.availability('dr-sara', working_day())['slots'][0]
    hold = service.prepare('p', 's', 'dr-sara', slot['start'], 'cleaning')
    with service.store.connection(write=True) as db:
        db.execute('UPDATE holds SET expires=0 WHERE id=?', (hold['hold_id'],))
    with pytest.raises(BookingError) as error:
        service.confirm('p', 's', hold['hold_id'])
    assert error.value.code == 'HOLD_EXPIRED'
    assert service.appointments('p')['appointments'] == []


def test_reschedule_preserves_original_if_new_hold_expires(service):
    slots = service.availability('dr-sara', working_day())['slots']
    hold = service.prepare('p', 's', 'dr-sara', slots[0]['start'], 'cleaning')
    original = service.confirm('p', 's', hold['hold_id'])['appointment_id']
    moved = service.prepare('p', 's', 'dr-sara', slots[1]['start'], 'cleaning', original)
    with service.store.connection(write=True) as db:
        db.execute('UPDATE holds SET expires=0 WHERE id=?', (moved['hold_id'],))
    with pytest.raises(BookingError):
        service.confirm('p', 's', moved['hold_id'])
    assert service.appointments('p')['appointments'][0]['start'] == slots[0]['start']


def test_reschedule_and_cancel_keep_owner_isolation(service):
    slots = service.availability('dr-sara', working_day())['slots']
    hold = service.prepare('p', 's', 'dr-sara', slots[0]['start'], 'cleaning')
    original = service.confirm('p', 's', hold['hold_id'])['appointment_id']
    moved = service.prepare('p', 's', 'dr-sara', slots[1]['start'], 'cleaning', original)
    assert service.confirm('p', 's', moved['hold_id'])['appointment_id'] == original
    assert service.appointments('p')['appointments'][0]['start'] == slots[1]['start']
    with pytest.raises(BookingError):
        service.cancel('other', original)
    service.cancel('p', original)
    assert service.cancel('p', original)['ok']
    assert service.appointments('p')['appointments'] == []
    assert slots[1]['start'] in {s['start'] for s in service.availability('dr-sara', working_day())['slots']}


def test_voice_requires_exact_confirmation(service):
    store = service.store
    store.create_session('s', 'p')
    dialog = VoiceDialog(store, service)
    dialog.turn('p', 's', 'book a cleaning')
    dialog.turn('p', 's', working_day())
    summary = dialog.turn('p', 's', 'first')
    assert summary['requires_confirmation']
    assert service.appointments('p')['appointments'] == []
    dialog.turn('p', 's', 'yes but at a different time')
    assert service.appointments('p')['appointments'] == []
    assert dialog.turn('p', 's', 'confirm')['ok']
    assert len(service.appointments('p')['appointments']) == 1


def test_no_releases_the_pending_slot(service):
    service.store.create_session('s', 'p')
    dialog = VoiceDialog(service.store, service)
    dialog.turn('p', 's', 'book cleaning')
    dialog.turn('p', 's', working_day())
    before = len(service.availability('dr-sara', working_day())['slots'])
    dialog.turn('p', 's', 'first')
    assert len(service.availability('dr-sara', working_day())['slots']) == before - 1
    dialog.turn('p', 's', 'no')
    assert len(service.availability('dr-sara', working_day())['slots']) == before


def test_ambiguous_relative_date_needs_clarification():
    assert resolve_day('next friday afternoon') is None
    assert resolve_day('tomorrow') is not None


def test_api_auth_and_session_isolation(client):
    assert client.get('/appointments').status_code == 401
    first, second = login(client), login(client)
    session = client.post('/voice/sessions', headers=first, json={}).json()['session_id']
    assert client.post(f'/voice/sessions/{session}/turn', headers=second, json={'transcript': 'my appointments'}).status_code == 404
    forged = {'Authorization': first['Authorization'][:-1] + 'z'}
    assert client.get('/me', headers=forged).status_code == 401


def test_livekit_token_microphone_grant_and_private_dispatch(client, monkeypatch):
    monkeypatch.setattr(settings, 'livekit_url', 'wss://test.livekit.cloud')
    monkeypatch.setattr(settings, 'livekit_api_key', 'test-key')
    monkeypatch.setattr(settings, 'livekit_api_secret', 'test-secret-at-least-32-characters')
    headers = login(client)
    session = client.post('/voice/sessions', headers=headers, json={}).json()['session_id']
    response = client.post(f'/voice/sessions/{session}/livekit', headers=headers, json={})
    assert response.status_code == 200
    claims = jwt.decode(response.json()['token'], settings.livekit_api_secret, algorithms=['HS256'])
    assert claims['video']['room'] == 'dental-' + session
    assert claims['video']['canPublishSources'] == ['microphone']
    assert claims['roomConfig']['agents'][0]['agentName'] == settings.livekit_agent_name
    assert 'service_token' not in claims.get('metadata', '')


def test_agent_tokens_cannot_access_patient_endpoints(client):
    token = mint_token({'scope': 'agent', 'uid': 'p', 'session_id': 's'})
    assert client.get('/appointments', headers={'Authorization': 'Bearer ' + token}).status_code == 401


def test_livekit_without_credentials_returns_actionable_error(client, monkeypatch):
    monkeypatch.setattr(settings, 'livekit_api_secret', '')
    headers = login(client)
    session = client.post('/voice/sessions', headers=headers, json={}).json()['session_id']
    assert client.post(f'/voice/sessions/{session}/livekit', headers=headers, json={}).status_code == 503


def test_production_refuses_demo_auth(monkeypatch):
    monkeypatch.setattr(settings, 'app_env', 'production')
    monkeypatch.setattr(settings, 'auth_mode', 'demo')
    monkeypatch.setattr(settings, 'booking_mode', 'local_demo')
    with pytest.raises(ValueError, match='Production requires'):
        settings.validate_runtime()


def test_real_booking_refuses_missing_service_account(monkeypatch):
    monkeypatch.setattr(settings, 'app_env', 'development')
    monkeypatch.setattr(settings, 'auth_mode', 'firebase')
    monkeypatch.setattr(settings, 'booking_mode', 'google_calendar')
    monkeypatch.setattr(settings, 'google_application_credentials', '')
    monkeypatch.setattr(settings, 'google_calendar_credentials', '')
    with pytest.raises(ValueError, match='service-account'):
        settings.validate_runtime()
