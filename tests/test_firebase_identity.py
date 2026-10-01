import time

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.config import settings
from app import firebase_identity
from app.security import current_patient


@pytest.fixture
def firebase_project(monkeypatch):
    monkeypatch.setattr(settings, 'auth_mode', 'firebase')
    monkeypatch.setattr(settings, 'booking_mode', 'local_demo')
    monkeypatch.setattr(settings, 'firebase_project_id', 'opeanapp')
    monkeypatch.setattr(settings, 'google_application_credentials', '')
    monkeypatch.setattr(settings, 'app_env', 'development')


@pytest.fixture
def signed_token(firebase_project, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setattr(firebase_identity.id_token, '_fetch_certs', lambda request, url: {'test-key': public_key})
    now = int(time.time())
    claims = {'iss': 'https://securetoken.google.com/opeanapp', 'aud': 'opeanapp', 'sub': 'google-patient',
              'iat': now - 10, 'auth_time': now - 20, 'exp': now + 3600,
              'email_verified': True, 'email': 'test@example.com', 'name': 'Test Patient',
              'firebase': {'sign_in_provider': 'google.com'}}

    def encode(**overrides):
        return jwt.encode({**claims, **overrides}, private_key, algorithm='RS256', headers={'kid': 'test-key'})
    return encode


def test_signed_google_token_is_verified_without_service_account(signed_token):
    credentials = HTTPAuthorizationCredentials(scheme='Bearer', credentials=signed_token())
    patient = current_patient(credentials)
    assert patient.uid == 'google-patient'
    assert patient.name == 'Test Patient'
    assert patient.email == 'test@example.com'


@pytest.mark.parametrize('overrides', [
    {'aud': 'another-project'},
    {'iss': 'https://securetoken.google.com/another-project'},
    {'exp': int(time.time()) - 30},
    {'auth_time': int(time.time()) + 3600},
    {'sub': ''},
    {'sub': 'a' * 129},
    {'email_verified': False},
    {'email_verified': 'true'},
    {'firebase': {'sign_in_provider': 'password'}},
])
def test_wrong_project_or_invalid_identity_is_rejected(signed_token, overrides):
    credentials = HTTPAuthorizationCredentials(scheme='Bearer', credentials=signed_token(**overrides))
    with pytest.raises(HTTPException) as error:
        current_patient(credentials)
    assert error.value.status_code == 401


def test_forged_signature_is_rejected(signed_token):
    token = signed_token()
    body, signature = token.rsplit('.', 1)
    signature = ('A' if signature[0] != 'A' else 'B') + signature[1:]
    with pytest.raises(HTTPException) as error:
        current_patient(HTTPAuthorizationCredentials(scheme='Bearer', credentials=body + '.' + signature))
    assert error.value.status_code == 401


def test_symmetric_token_is_rejected(firebase_project):
    token = jwt.encode({'sub': 'forged'}, 'attacker-secret-with-at-least-32-characters', algorithm='HS256', headers={'kid': 'test-key'})
    with pytest.raises(HTTPException) as error:
        current_patient(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
    assert error.value.status_code == 401


def test_production_requires_admin_credential_for_revocation(firebase_project, monkeypatch):
    monkeypatch.setattr(settings, 'app_env', 'production')
    monkeypatch.setattr(settings, 'app_signing_secret', 'production-key-with-at-least-32-characters')
    with pytest.raises(ValueError, match='revocation checks'):
        settings.validate_runtime()
