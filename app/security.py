import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import BASE_DIR, settings

bearer = HTTPBearer(auto_error=False)


@dataclass
class Patient:
    uid: str
    name: str
    email: str = ''


def signing_key() -> str:
    if settings.app_signing_secret:
        return settings.app_signing_secret
    key_file = BASE_DIR / 'data' / '.signing-key'
    key_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with key_file.open('x') as f:
            f.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass
    return key_file.read_text().strip()


def mint_token(payload: dict, ttl: int = 3600) -> str:
    body = base64.urlsafe_b64encode(json.dumps({**payload, 'exp': int(time.time()) + ttl}).encode()).decode()
    signature = hmac.new(signing_key().encode(), body.encode(), hashlib.sha256).hexdigest()
    return f'{body}.{signature}'


def decode_token(token: str, scope: str) -> dict:
    try:
        body, signature = token.split('.')
        expected = hmac.new(signing_key().encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(body))
        if payload['exp'] <= time.time() or payload['scope'] != scope:
            raise ValueError()
        return payload
    except (ValueError, KeyError, TypeError):
        raise HTTPException(401, 'Session expired. Please start a new conversation.') from None


def credentials_token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if not credentials:
        raise HTTPException(401, 'Start a conversation to create a session.')
    return credentials.credentials


def current_patient(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Patient:
    token = credentials_token(credentials)
    if settings.auth_mode in {'demo', 'guest'}:
        claims = decode_token(token, 'guest' if settings.auth_mode == 'guest' else 'patient')
        return Patient(claims['uid'], claims['name'])
    try:
        from .firebase_identity import verify_firebase_identity
        claims = verify_firebase_identity(token)
        email = claims.get('email', '')
        return Patient(claims['uid'], claims.get('name') or email.split('@')[0] or 'Patient', email)
    except Exception:
        raise HTTPException(401, 'Google sign-in could not be verified. Please sign in again.') from None
