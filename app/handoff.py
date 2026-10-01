"""Persist a staff follow-up without promising an immediate live transfer."""
import hashlib
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def request_handoff(store, service, uid, session_id, code):
    request_id = hashlib.sha256(f'{uid}:{session_id}'.encode()).hexdigest()
    record = {'id': request_id, 'patient_id': uid, 'session_id': session_id,
              'reason_code': code, 'status': 'pending',
              'updated_at': datetime.now(timezone.utc).isoformat()}
    try:
        if hasattr(service, 'db'):
            service.db.collection('human_followups').document(request_id).set(record, merge=True)
        else:
            store.save_followup(record)
    except Exception:
        logger.exception('Could not persist clinic follow-up')
        return {'ok': False, 'code': code, 'handoff_requested': False, 'retryable': False,
                'message': 'I could not complete your request or notify the clinic team. Please contact reception for help. Check your appointments before trying another booking.'}
    return {'ok': False, 'code': code, 'handoff_requested': True, 'retryable': False,
            'message': 'I could not complete your request. I have asked our clinic team to contact you and guide you. Please check your appointments before trying another booking.'}
