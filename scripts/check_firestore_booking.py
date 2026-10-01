"""Exercise real Firestore transactions in isolated, temporary verification collections."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from app.appointments import BookingError
from app.firestore_appointments import FirestoreAppointmentService


def main():
    service = FirestoreAppointmentService()
    real_db = service.db
    prefix = 'verify_guest_' + uuid.uuid4().hex + '_'
    collections = set()

    class IsolatedDB:
        def collection(self, name):
            collections.add(prefix + name)
            return real_db.collection(prefix + name)

        def transaction(self):
            return real_db.transaction()

        def batch(self):
            return real_db.batch()

    service.db = IsolatedDB()
    try:
        doctor = service.public_doctors()[0]['id']
        slots = []
        for offset in range(1, 8):
            day = (datetime.now(service.timezone) + timedelta(days=offset)).date().isoformat()
            slots = service.availability(doctor, day)['slots']
            if len(slots) >= 2:
                break
        assert len(slots) >= 2, 'No configured working slots in the next week'
        contact = {'client_name': 'Verification Test', 'phone_number': '+12025550123'}

        def reserve(i):
            try:
                return i, service.prepare(f'test-{i}', f'session-{i}', doctor, slots[0]['start'], **contact)
            except BookingError as exc:
                assert exc.code == 'SLOT_UNAVAILABLE'
                return i, None
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = list(pool.map(reserve, range(2)))
        winners = [(i, r) for i, r in attempts if r]
        assert len(winners) == 1, 'Duplicate holds accepted'
        winner, hold = winners[0]
        uid, session = f'test-{winner}', f'session-{winner}'
        try:
            service.confirm('other-user', session, hold['hold_id'])
            raise AssertionError('Wrong owner was allowed to confirm')
        except BookingError as exc:
            assert exc.code == 'HOLD_EXPIRED'
        booked = service.confirm(uid, session, hold['hold_id'])
        assert service.confirm(uid, session, hold['hold_id']) == booked
        doc = service.db.collection('appointments').document(booked['appointment_id']).get().to_dict()
        assert doc['client_name'] == contact['client_name'] and doc['phone_number'] == contact['phone_number']
        assert doc['appointment_date'] == day and doc['appointment_time']
        assert not any(k in doc for k in ('google_calendar_event_id', 'customer_email', 'confirmation_email_status'))
        assert slots[0]['start'] not in [s['start'] for s in service.availability(doctor, day)['slots']]
        assert len(service.appointments(uid)['appointments']) == 1
        assert service.appointments('other-user')['appointments'] == []
        moved = service.prepare(uid, session, doctor, slots[1]['start'], appointment_id=booked['appointment_id'], **contact)
        assert service.confirm(uid, session, moved['hold_id'])['appointment_id'] == booked['appointment_id']
        service.cancel(uid, booked['appointment_id'])
        assert service.appointments(uid)['appointments'] == []
        assert all(s['start'] in [item['start'] for item in service.availability(doctor, day)['slots']] for s in slots[:2])
        print('PASS: Firebase persistence, contact/date/time, concurrent slot protection, owner isolation, idempotent confirmation, rescheduling and cancellation.')
    finally:
        for name in collections:
            for snapshot in real_db.collection(name).stream():
                snapshot.reference.delete()
        print('Temporary verification records removed.')


if __name__ == '__main__':
    main()
