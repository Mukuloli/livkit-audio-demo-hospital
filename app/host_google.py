"""Server-side Google OAuth for doctors and companies."""
import base64
import hashlib
import time
from threading import Lock
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken
from firebase_admin import firestore

from .appointments import BookingError, result
from .config import settings
from .security import Patient, signing_key


GOOGLE_SCOPES = (
    'openid',
    'email',
    'profile',
    'https://www.googleapis.com/auth/calendar.events',
)


class HostGoogleOAuth:
    """Stores encrypted host refresh tokens and returns short-lived access tokens."""

    def __init__(self, db):
        self.db = db
        raw_key = settings.oauth_token_encryption_key.strip()
        if raw_key:
            try:
                self.cipher = Fernet(raw_key.encode())
            except (ValueError, TypeError) as exc:
                raise ValueError('OAUTH_TOKEN_ENCRYPTION_KEY must be a valid Fernet key.') from exc
        else:
            derived = hashlib.sha256(('noor-host-google:' + signing_key()).encode()).digest()
            self.cipher = Fernet(base64.urlsafe_b64encode(derived))
        self._cache = {}
        self._lock = Lock()

    @property
    def configured(self):
        return bool(settings.google_oauth_client_id and settings.google_oauth_client_secret)

    def require_configured(self):
        if not self.configured:
            raise BookingError(
                'HOST_OAUTH_NOT_CONFIGURED',
                'The platform Google OAuth client is not configured yet.',
            )

    def require_host_admin(self, patient: Patient):
        allowed = {value.strip().lower() for value in settings.host_admin_emails.split(',') if value.strip()}
        if not patient.email or patient.email.lower() not in allowed:
            raise BookingError('HOST_ACCESS_DENIED', 'This account is not authorized to manage clinic calendars.')

    def connection(self, doctor_id):
        snapshot = self.db.collection('calendar_connections').document(doctor_id).get()
        if not snapshot.exists:
            raise BookingError(
                'HOST_CALENDAR_NOT_CONNECTED',
                'This doctor has not connected a Google Calendar yet.',
            )
        data = snapshot.to_dict()
        if data.get('status') != 'connected' or not data.get('encrypted_refresh_token'):
            raise BookingError('HOST_CALENDAR_NOT_CONNECTED', 'The doctor must reconnect Google Calendar.')
        return data

    def status(self, doctor_id):
        snapshot = self.db.collection('calendar_connections').document(doctor_id).get()
        if not snapshot.exists:
            return result('Google Calendar is not connected.', connected=False, doctor_id=doctor_id)
        data = snapshot.to_dict()
        return result(
            'Google Calendar connection status.',
            connected=data.get('status') == 'connected',
            doctor_id=doctor_id,
            google_email=data.get('google_email', ''),
            calendar_id=data.get('calendar_id', 'primary'),
            connected_at=data.get('connected_at').isoformat() if hasattr(data.get('connected_at'), 'isoformat') else None,
        )

    def authorization_url(self, state, login_hint=''):
        self.require_configured()
        query = {
            'client_id': settings.google_oauth_client_id,
            'redirect_uri': settings.google_oauth_redirect_uri,
            'response_type': 'code',
            'scope': ' '.join(GOOGLE_SCOPES),
            'access_type': 'offline',
            'include_granted_scopes': 'true',
            'prompt': 'consent',
            'state': state,
        }
        if login_hint:
            query['login_hint'] = login_hint
        return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(query)

    def exchange_and_store(self, doctor_id, code):
        self.require_configured()
        try:
            response = requests.post('https://oauth2.googleapis.com/token', data={
                'code': code,
                'client_id': settings.google_oauth_client_id,
                'client_secret': settings.google_oauth_client_secret,
                'redirect_uri': settings.google_oauth_redirect_uri,
                'grant_type': 'authorization_code',
            }, timeout=20)
        except requests.RequestException:
            raise BookingError('GOOGLE_OAUTH_UNAVAILABLE', 'Google OAuth could not be reached. Please retry.') from None
        if not response.ok:
            raise BookingError('GOOGLE_OAUTH_FAILED', 'Google did not accept the Calendar connection. Please retry.')
        token = response.json()
        refresh_token = token.get('refresh_token')
        if not refresh_token:
            raise BookingError('GOOGLE_REFRESH_TOKEN_MISSING', 'Google did not return offline access. Reconnect and approve access.')
        try:
            profile_response = requests.get(
                'https://openidconnect.googleapis.com/v1/userinfo',
                headers={'Authorization': 'Bearer ' + token['access_token']}, timeout=15,
            )
        except requests.RequestException:
            raise BookingError('GOOGLE_OAUTH_UNAVAILABLE', 'Google account details could not be read. Please retry.') from None
        if not profile_response.ok:
            raise BookingError('GOOGLE_OAUTH_FAILED', 'Google account details could not be verified.')
        profile = profile_response.json()
        scopes = set(token.get('scope', '').split())
        required = {'https://www.googleapis.com/auth/calendar.events'}
        if not required.issubset(scopes):
            raise BookingError('GOOGLE_SCOPES_MISSING', 'Calendar and email permissions are required for automatic booking.')
        encrypted = self.cipher.encrypt(refresh_token.encode()).decode()
        self.db.collection('calendar_connections').document(doctor_id).set({
            'doctor_id': doctor_id,
            'provider': 'google',
            'google_account_id': profile.get('sub', ''),
            'google_email': profile.get('email', ''),
            'calendar_id': 'primary',
            'encrypted_refresh_token': encrypted,
            'scopes': sorted(scopes),
            'status': 'connected',
            'connected_at': firestore.SERVER_TIMESTAMP,
            'updated_at': firestore.SERVER_TIMESTAMP,
        }, merge=True)
        self._cache.pop(doctor_id, None)
        return result('Doctor Google Calendar and email are connected.', connected=True, google_email=profile.get('email', ''))

    def access_token(self, doctor_id):
        self.require_configured()
        with self._lock:
            cached = self._cache.get(doctor_id)
            if cached and cached['expires_at'] > time.time() + 60:
                return cached['token'], cached['connection']
            connection = self.connection(doctor_id)
            try:
                refresh_token = self.cipher.decrypt(connection['encrypted_refresh_token'].encode()).decode()
            except (InvalidToken, ValueError, TypeError):
                raise BookingError('HOST_TOKEN_INVALID', 'The doctor must reconnect Google Calendar.') from None
            try:
                response = requests.post('https://oauth2.googleapis.com/token', data={
                    'client_id': settings.google_oauth_client_id,
                    'client_secret': settings.google_oauth_client_secret,
                    'refresh_token': refresh_token,
                    'grant_type': 'refresh_token',
                }, timeout=20)
            except requests.RequestException:
                raise BookingError('GOOGLE_OAUTH_UNAVAILABLE', 'Google authorization is temporarily unavailable. Please retry.') from None
            if not response.ok:
                raise BookingError('HOST_TOKEN_EXPIRED', 'The doctor must reconnect Google Calendar.')
            payload = response.json()
            cached = {
                'token': payload['access_token'],
                'expires_at': time.time() + int(payload.get('expires_in', 3600)),
                'connection': connection,
            }
            self._cache[doctor_id] = cached
            return cached['token'], connection

    def disconnect(self, doctor_id):
        snapshot = self.db.collection('calendar_connections').document(doctor_id).get()
        if snapshot.exists:
            data = snapshot.to_dict()
            try:
                refresh_token = self.cipher.decrypt(data['encrypted_refresh_token'].encode()).decode()
                requests.post('https://oauth2.googleapis.com/revoke', data={'token': refresh_token}, timeout=10)
            except (KeyError, InvalidToken, requests.RequestException):
                pass
            snapshot.reference.delete()
        self._cache.pop(doctor_id, None)
        return result('Google Calendar connection removed.', connected=False, doctor_id=doctor_id)
