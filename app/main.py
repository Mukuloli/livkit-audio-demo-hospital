import json
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from .appointments import AppointmentService, BookingError, DOCTORS, result
from .config import settings
from .dialog import VoiceDialog
from .handoff import request_handoff
from .security import Patient, bearer, credentials_token, current_patient, decode_token, mint_token, signing_key
from .store import Store


@asynccontextmanager
async def lifespan(app):
    settings.validate_runtime()
    signing_key()
    app.state.store = Store()
    if settings.booking_mode in {'firestore', 'firestore_calendar'}:
        from .firestore_appointments import FirestoreAppointmentService
        app.state.service = FirestoreAppointmentService()
    elif settings.booking_mode == 'google_calendar':
        from .real_appointments import GoogleCalendarAppointmentService
        app.state.service = GoogleCalendarAppointmentService()
    else:
        app.state.service = AppointmentService(app.state.store)
    app.state.dialog = VoiceDialog(app.state.store, app.state.service)
    yield


app = FastAPI(title='Dental Voice API', version='0.1.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.frontend_origins.split(','),
                   allow_credentials=False, allow_methods=['GET', 'POST'], allow_headers=['Authorization', 'Content-Type'])


class DemoLogin(BaseModel):
    name: str = Field(default='Demo patient', min_length=1, max_length=80)


class Turn(BaseModel):
    transcript: str = Field(min_length=1, max_length=2000)


class ToolCall(BaseModel):
    name: str
    arguments: dict = Field(default_factory=dict)


@app.get('/health')
def health():
    storage = {'firestore': 'firestore', 'firestore_calendar': 'firestore+google-calendar-invites',
               'google_calendar': 'firestore+google-calendar'}.get(settings.booking_mode, 'sqlite-demo')
    return {'ok': True, 'storage': storage, 'booking_mode': settings.booking_mode,
            'host_google_oauth_configured': settings.host_google_oauth_ready,
            'calendar_invitations_enabled': settings.calendar_invitations_enabled}


@app.get('/config')
def configuration():
    doctors = app.state.service.public_doctors() if hasattr(app.state.service, 'public_doctors') else DOCTORS
    return {'auth_mode': settings.auth_mode, 'livekit_configured': settings.livekit_ready,
            'voice_pipeline': settings.voice_pipeline, 'clinic_timezone': settings.clinic_timezone,
            'booking_mode': settings.booking_mode, 'calendar_invitations_enabled': settings.calendar_invitations_enabled,
            'doctors': doctors}


@app.post('/auth/demo')
def demo_login(body: DemoLogin):
    if settings.auth_mode != 'demo' or settings.app_env == 'production':
        raise HTTPException(404, 'Demo authentication is disabled.')
    uid = 'demo-' + str(uuid.uuid4())
    token = mint_token({'scope': 'patient', 'uid': uid, 'name': body.name}, ttl=86400)
    return {'token': token, 'patient': {'uid': uid, 'name': body.name}, 'expires_in': 86400}


@app.get('/me')
def me(patient: Patient = Depends(current_patient)):
    return {'uid': patient.uid, 'name': patient.name, 'email': patient.email}


@app.post('/auth/guest')
def guest_session():
    if settings.auth_mode != 'guest':
        raise HTTPException(404, 'Guest sessions are disabled.')
    uid = 'guest-' + str(uuid.uuid4())
    token = mint_token({'scope': 'guest', 'uid': uid, 'name': 'Guest'}, ttl=86400)
    return {'token': token, 'patient': {'uid': uid, 'name': 'Guest'}, 'expires_in': 86400}


@app.get('/appointments')
def appointments(patient: Patient = Depends(current_patient)):
    return app.state.service.appointments(patient.uid)


@app.get('/availability')
def availability(doctor_id: str = Query(min_length=1, max_length=100),
                 date: str = Query(min_length=10, max_length=10),
                 patient: Patient = Depends(current_patient)):
    try:
        return app.state.service.availability(doctor_id, date)
    except BookingError as exc:
        raise HTTPException(409, exc.message) from None


def host_google_patient(credentials=Depends(bearer)):
    token = credentials_token(credentials)
    try:
        from .firebase_identity import verify_firebase_identity
        claims = verify_firebase_identity(token)
        patient = Patient(claims['uid'], claims.get('name', 'Clinic admin'), claims.get('email', ''))
        app.state.service.host_google.require_host_admin(patient)
        return patient
    except BookingError as exc:
        raise HTTPException(403, exc.message) from None
    except Exception:
        raise HTTPException(401, 'Sign in with the allowlisted clinic admin Google account.') from None


@app.get('/host/google/status')
def host_google_status(doctor_id: str = Query(min_length=1, max_length=100),
                       patient: Patient = Depends(host_google_patient)):
    if settings.booking_mode not in {'google_calendar', 'firestore_calendar'} or not settings.calendar_invitations_enabled:
        raise HTTPException(409, 'Host Google Calendar sync is not enabled.')
    try:
        app.state.service.doctor(doctor_id)
        app.state.service.host_google.require_host_admin(patient)
        status = app.state.service.host_google.status(doctor_id)
        return {**status, 'oauth_configured': settings.host_google_oauth_ready}
    except BookingError as exc:
        raise HTTPException(403 if exc.code == 'HOST_ACCESS_DENIED' else 409, exc.message) from None


@app.get('/host/google/connect')
def host_google_connect(doctor_id: str = Query(min_length=1, max_length=100),
                        patient: Patient = Depends(host_google_patient)):
    if settings.booking_mode not in {'google_calendar', 'firestore_calendar'} or not settings.calendar_invitations_enabled:
        raise HTTPException(409, 'Host Google Calendar sync is not enabled.')
    try:
        app.state.service.doctor(doctor_id)
        app.state.service.host_google.require_host_admin(patient)
        state = mint_token({'scope': 'host_oauth_state', 'uid': patient.uid,
                            'email': patient.email, 'doctor_id': doctor_id}, ttl=600)
        return {'authorization_url': app.state.service.host_google.authorization_url(state, patient.email)}
    except BookingError as exc:
        raise HTTPException(403 if exc.code == 'HOST_ACCESS_DENIED' else 409, exc.message) from None


@app.get('/host/google/callback')
def host_google_callback(code: str = '', state: str = '', error: str = ''):
    if settings.booking_mode not in {'google_calendar', 'firestore_calendar'} or not settings.calendar_invitations_enabled:
        raise HTTPException(404, 'Google integration is disabled.')
    redirect_base = settings.frontend_public_url.rstrip('/') + '/admin'
    if error:
        return RedirectResponse(redirect_base + '?' + urlencode({'host_calendar': 'denied'}), status_code=302)
    try:
        claims = decode_token(state, 'host_oauth_state')
        app.state.service.doctor(claims['doctor_id'])
        connected = app.state.service.host_google.exchange_and_store(claims['doctor_id'], code)
        query = {'host_calendar': 'connected'}
    except (BookingError, HTTPException, KeyError):
        query = {'host_calendar': 'failed'}
    return RedirectResponse(redirect_base + '?' + urlencode(query), status_code=302)


@app.post('/host/google/disconnect')
def host_google_disconnect(doctor_id: str = Query(min_length=1, max_length=100),
                           patient: Patient = Depends(host_google_patient)):
    if settings.booking_mode not in {'google_calendar', 'firestore_calendar'} or not settings.calendar_invitations_enabled:
        raise HTTPException(404, 'Google integration is disabled.')
    if settings.booking_mode != 'google_calendar':
        raise HTTPException(404, 'Google integration is disabled.')
    try:
        app.state.service.doctor(doctor_id)
        app.state.service.host_google.require_host_admin(patient)
        return app.state.service.host_google.disconnect(doctor_id)
    except BookingError as exc:
        raise HTTPException(403 if exc.code == 'HOST_ACCESS_DENIED' else 409, exc.message) from None


@app.post('/voice/sessions')
def create_session(patient: Patient = Depends(current_patient)):
    session_id = str(uuid.uuid4())
    app.state.store.create_session(session_id, patient.uid)
    mode_message = ('I can book an appointment using your name, phone number, date and time.'
                    if settings.booking_mode in {'firestore', 'firestore_calendar'} and settings.calendar_invitations_enabled else 'Your confirmed appointment details will be sent by email.'
                    if settings.booking_mode == 'google_calendar' else 'Your appointment will be saved securely.'
                    if settings.booking_mode in {'firestore', 'firestore_calendar'} else 'This is a local test clinic; no actual calendar event or email is sent.')
    return {'session_id': session_id,
            'message': f'Hi {patient.name}. I am Noor, your dental appointment assistant. {mode_message} How can I help you today?'}


def require_session(session_id, uid):
    if app.state.store.get_session(session_id, uid) is None:
        raise HTTPException(404, 'Voice session not found. Start a new session.')


@app.post('/voice/sessions/{session_id}/turn')
def turn(session_id: str, body: Turn, patient: Patient = Depends(current_patient)):
    require_session(session_id, patient.uid)
    return app.state.dialog.turn(patient.uid, session_id, body.transcript, patient.email)


@app.post('/voice/sessions/{session_id}/end')
def end(session_id: str, patient: Patient = Depends(current_patient)):
    require_session(session_id, patient.uid)
    app.state.service.release(patient.uid, session_id)
    app.state.store.save_session(session_id, patient.uid, {})
    return result('Voice session ended.')


@app.post('/voice/sessions/{session_id}/livekit')
def livekit_session(session_id: str, patient: Patient = Depends(current_patient)):
    require_session(session_id, patient.uid)
    if not settings.livekit_ready:
        raise HTTPException(503, 'Add LiveKit credentials to backend/.env, then restart the API and agent.')
    from livekit import api
    room = 'dental-' + session_id
    agent_token = mint_token({'scope': 'agent', 'uid': patient.uid, 'session_id': session_id}, ttl=7200)
    metadata = json.dumps({'session_id': session_id, 'patient_name': patient.name,
                           'patient_email': patient.email, 'service_token': agent_token})
    token = (api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
             .with_identity(patient.uid).with_name(patient.name).with_ttl(timedelta(minutes=10))
             .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True,
                                         can_publish_sources=['microphone'], can_subscribe=True, can_publish_data=False))
             .with_room_config(api.RoomConfiguration(name=room, empty_timeout=120, max_participants=2,
                 agents=[api.RoomAgentDispatch(agent_name=settings.livekit_agent_name, metadata=metadata)]))
             .to_jwt())
    return {'server_url': settings.livekit_url, 'token': token, 'room_name': room}


@app.post('/internal/voice/{session_id}/tools')
def agent_tool(session_id: str, body: ToolCall, credentials=Depends(bearer)):
    import logging
    log = logging.getLogger('noor.tools')
    log.info('[TOOL IN] name=%s  args=%s  session=%s', body.name, body.arguments, session_id)
    claims = decode_token(credentials_token(credentials), 'agent')
    if claims['session_id'] != session_id:
        raise HTTPException(403, 'Service token belongs to another session.')
    uid = claims['uid']
    require_session(session_id, uid)
    state = app.state.store.get_session(session_id, uid)
    service = app.state.service
    args = body.arguments
    try:
        if body.name == 'get_appointments':
            return service.appointments(uid)
        if body.name == 'get_doctors':
            doctors = service.public_doctors() if hasattr(service, 'public_doctors') else DOCTORS
            return result('Connected clinic doctors.', doctors=doctors)
        if body.name == 'check_availability':
            return service.availability(args['doctor_id'], args['date'])
        if body.name == 'prepare_booking':
            contact = ({'client_name': args.get('client_name', ''), 'phone_number': args.get('phone_number', ''),
                        'client_email': args.get('client_email', '')}
                       if settings.booking_mode in {'firestore', 'firestore_calendar'} else {})
            log.info('[PREPARE] contact=%s  doctor=%s  start=%s', contact, args.get('doctor_id'), args.get('start'))
            response = service.prepare(uid, session_id, args['doctor_id'], args['start'], args.get('reason', 'Appointment'), args.get('appointment_id'), **contact)
            state = {'hold_id': response['hold_id'], 'stage': 'confirmation'}
        elif body.name == 'prepare_cancellation':
            own = service.appointments(uid)['appointments']
            target = next((a for a in own if a['id'] == args['appointment_id']), None)
            if not target:
                raise BookingError('NOT_FOUND', 'That upcoming appointment was not found.')
            state = {'stage': 'cancel_confirmation', 'appointment_id': target['id']}
            response = result('Read this appointment back and ask the patient to confirm cancellation.', appointment=target, requires_confirmation=True)
        elif body.name == 'confirm_action':
            # The agent calls this only after reading the exact pending summary and hearing confirmation.
            if state.get('stage') == 'confirmation':
                response = service.confirm(uid, session_id, state['hold_id'])
            elif state.get('stage') == 'cancel_confirmation':
                response = service.cancel(uid, state['appointment_id'])
            else:
                raise BookingError('NO_PENDING_ACTION', 'Prepare a booking or cancellation first and ask for confirmation.')
            state = {}
        elif body.name == 'release_hold':
            response = service.release(uid, session_id)
            state = {}
        else:
            raise HTTPException(400, 'Unknown voice tool.')
        app.state.store.save_session(session_id, uid, state)
        log.info('[TOOL OUT] name=%s  ok=%s  message=%s', body.name, response.get('ok'), str(response.get('message', ''))[:200])
        return response
    except BookingError as exc:
        log.warning('[BOOKING ERROR] name=%s  code=%s  message=%s', body.name, exc.code, exc.message)
        if settings.booking_mode in {'firestore', 'firestore_calendar'} and exc.code in {
                'INVALID_NAME', 'INVALID_PHONE', 'INVALID_EMAIL', 'INVALID_DATE', 'INVALID_TIME', 'SLOT_UNAVAILABLE',
                'HOLD_EXPIRED', 'NOT_FOUND', 'NO_PENDING_ACTION', 'UNKNOWN_DOCTOR'}:
            return {'ok': False, 'code': exc.code, 'message': exc.message, 'retryable': True}
        return request_handoff(app.state.store, service, uid, session_id, exc.code)
    except (KeyError, TypeError) as exc:
        log.error('[TOOL ARGS ERROR] name=%s  args=%s  error=%s', body.name, body.arguments, exc)
        raise HTTPException(422, 'Missing or invalid tool arguments.') from None
    except HTTPException:
        raise
    except Exception as exc:
        log.exception('[TOOL EXCEPTION] name=%s  error=%s', body.name, exc)
        return request_handoff(app.state.store, service, uid, session_id, 'BOOKING_SERVICE_UNAVAILABLE')



@app.get('/host/followups')
def host_followups(patient: Patient = Depends(current_patient)):
    if settings.booking_mode != 'google_calendar':
        raise HTTPException(409, 'Staff follow-ups require the connected clinic.')
    service = app.state.service
    try:
        service.host_google.require_host_admin(patient)
    except BookingError as exc:
        raise HTTPException(403, exc.message) from None
    from firebase_admin import auth
    from google.cloud.firestore_v1.base_query import FieldFilter
    records = []
    for snapshot in service.db.collection('human_followups').where(filter=FieldFilter('status', '==', 'pending')).limit(100).stream():
        record = snapshot.to_dict()
        try:
            customer = auth.get_user(record['patient_id'], app=service.admin_app)
            record.update(customer_name=customer.display_name, customer_email=customer.email)
        except Exception:
            record.update(customer_name='Patient', customer_email='')
        records.append(record)
    return {'followups': records}
