from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# Local development is started from shells that may still contain credentials
# from an older LiveKit project. The checked-out backend .env is the explicit
# source selected for this app, so load it before Pydantic reads the process
# environment.
load_dotenv(BASE_DIR / '.env', override=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / '.env', extra='ignore')
    app_env: str = 'development'
    auth_mode: str = 'demo'
    frontend_origins: str = 'http://localhost:3000'
    database_path: str = 'data/voice-demo.db'
    app_signing_secret: str = ''
    livekit_url: str = ''
    livekit_api_key: str = ''
    livekit_api_secret: str = ''
    livekit_agent_name: str = 'dental-voice'
    backend_url: str = 'http://127.0.0.1:8000'
    frontend_public_url: str = 'http://localhost:3000'
    voice_pipeline: str = 'stt_tts'
    stt_model: str = 'deepgram/nova-3'
    stt_language: str = 'multi'
    llm_model: str = 'google/gemini-2.5-flash'
    tts_model: str = 'cartesia/sonic-3'
    tts_voice: str = '794f9389-aac1-45b6-b726-9d9369183238'
    google_api_key: str = ''
    gemini_model: str = 'gemini-3.8-flash'
    gemini_live_model: str = 'gemini-3.1-flash-live-preview'
    gemini_voice: str = 'Puck'
    firebase_project_id: str = ''
    google_application_credentials: str = ''
    firestore_database_id: str = '(default)'
    booking_mode: str = 'local_demo'
    google_calendar_credentials: str = ''
    google_calendar_id: str = ''
    google_oauth_client_id: str = ''
    google_oauth_client_secret: str = ''
    google_oauth_redirect_uri: str = 'http://127.0.0.1:8000/host/google/callback'
    oauth_token_encryption_key: str = ''
    host_admin_emails: str = ''
    google_meet_enabled: bool = False
    confirmation_email_enabled: bool = True
    calendar_invitations_enabled: bool = False
    appointment_location: str = ''
    clinic_data_path: str = ''
    doctor_id: str = 'primary-doctor'
    doctor_name: str = 'Primary doctor'
    doctor_specialty: str = 'General dentistry'
    doctor_services: str = 'checkup,cleaning,whitening'
    clinic_timezone: str = 'Asia/Dubai'

    @property
    def db_path(self) -> Path:
        path = Path(self.database_path)
        return path if path.is_absolute() else BASE_DIR / path

    @property
    def livekit_ready(self) -> bool:
        return bool(self.livekit_url and self.livekit_api_key and self.livekit_api_secret)

    @property
    def host_google_oauth_ready(self) -> bool:
        return bool(self.google_oauth_client_id and self.google_oauth_client_secret and self.google_oauth_redirect_uri)

    def validate_runtime(self):
        if self.auth_mode not in {'demo', 'firebase', 'guest'}:
            raise ValueError('AUTH_MODE must be demo, firebase or guest')
        if self.voice_pipeline not in {'stt_tts', 'gemini_live'}:
            raise ValueError('VOICE_PIPELINE must be stt_tts or gemini_live')
        if self.booking_mode not in {'local_demo', 'google_calendar', 'firestore', 'firestore_calendar'}:
            raise ValueError('BOOKING_MODE must be local_demo, google_calendar, firestore or firestore_calendar')
        if self.booking_mode in {'firestore', 'firestore_calendar'}:
            if not self.firebase_project_id or not Path(self.google_application_credentials).is_file():
                raise ValueError('Firestore booking requires a Firebase project and service-account JSON file')
        if self.calendar_invitations_enabled and not self.host_google_oauth_ready:
            raise ValueError('Calendar invitations require Google OAuth web client credentials')
        if self.auth_mode == 'firebase' and not self.firebase_project_id:
            raise ValueError('Firebase authentication requires FIREBASE_PROJECT_ID')
        if self.booking_mode == 'google_calendar':
            if self.auth_mode != 'firebase':
                raise ValueError('Real booking requires AUTH_MODE=firebase')
            credential_path = self.google_application_credentials
            if not credential_path or not Path(credential_path).is_file():
                raise ValueError('Real booking requires a Firebase service-account JSON file')
            if not self.clinic_data_path and self.doctor_name == 'Primary doctor':
                raise ValueError('Real booking requires an actual DOCTOR_NAME')
        if self.app_env == 'production' and (self.auth_mode not in {'firebase', 'guest'} or len(self.app_signing_secret) < 32):
            raise ValueError('Production requires Firebase auth and APP_SIGNING_SECRET of at least 32 characters')
        if self.app_env == 'production' and not self.google_application_credentials:
            raise ValueError('Production requires GOOGLE_APPLICATION_CREDENTIALS for Firebase revocation checks')
        if self.app_env == 'production' and self.booking_mode == 'google_calendar':
            if not self.host_google_oauth_ready:
                raise ValueError('Production booking requires the platform Google OAuth client')
            if not self.host_admin_emails:
                raise ValueError('Production booking requires HOST_ADMIN_EMAILS')


settings = Settings()
