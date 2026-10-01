"""Verify Firebase ID tokens; local public-key verification needs no private key."""
import threading
import time

import jwt
from cachecontrol import CacheControl
from google.auth.transport.requests import Request
from google.oauth2 import id_token
import requests

from .config import settings

_local = threading.local()
_admin_lock = threading.Lock()


class CertificateRequest(Request):
    def __call__(self, *args, **kwargs):
        kwargs['timeout'] = 10
        return super().__call__(*args, **kwargs)


def certificate_request():
    if not hasattr(_local, 'request'):
        _local.request = CertificateRequest(session=CacheControl(requests.Session()))
    return _local.request


def verify_public_token(token: str) -> dict:
    header = jwt.get_unverified_header(token)
    if header.get('alg') != 'RS256' or not header.get('kid'):
        raise ValueError('Invalid Firebase signing algorithm or key ID')
    claims = id_token.verify_firebase_token(token, certificate_request(), audience=settings.firebase_project_id)
    return validate_claims(claims)


def validate_claims(claims: dict) -> dict:
    now = time.time()
    subject = claims.get('sub')
    if claims.get('iss') != f'https://securetoken.google.com/{settings.firebase_project_id}':
        raise ValueError('Wrong token issuer')
    if claims.get('aud') != settings.firebase_project_id:
        raise ValueError('Wrong token audience')
    if not isinstance(subject, str) or not 0 < len(subject) <= 128:
        raise ValueError('Invalid Firebase user ID')
    for field in ('iat', 'auth_time', 'exp'):
        if not isinstance(claims.get(field), (float, int)) or isinstance(claims[field], bool):
            raise ValueError('Missing Firebase time claims')
    if claims['exp'] <= now or claims['iat'] > now or claims['auth_time'] > now:
        raise ValueError('Expired token or invalid authentication time')
    if claims.get('email_verified') is not True or not claims.get('email'):
        raise ValueError('Verified email required')
    if claims.get('firebase', {}).get('sign_in_provider') != 'google.com':
        raise ValueError('Google sign-in required')
    return {**claims, 'uid': subject}


def verify_firebase_identity(token: str) -> dict:
    if not settings.google_application_credentials and settings.app_env != 'production':
        return verify_public_token(token)
    import firebase_admin
    from firebase_admin import auth, credentials
    with _admin_lock:
        try:
            app = firebase_admin.get_app('dental-voice-auth')
        except ValueError:
            credential = credentials.Certificate(settings.google_application_credentials)
            app = firebase_admin.initialize_app(credential, {'projectId': settings.firebase_project_id}, name='dental-voice-auth')
    return validate_claims(auth.verify_id_token(token, app=app, check_revoked=True))
