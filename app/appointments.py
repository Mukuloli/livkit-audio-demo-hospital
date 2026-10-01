import json
import time
import uuid
from datetime import date, datetime, time as clock_time, timedelta, timezone
from zoneinfo import ZoneInfo

from .store import Store

DUBAI = ZoneInfo('Asia/Dubai')
DOCTORS = [
    {'id': 'dr-sara', 'name': 'Dr. Sara Ahmed', 'specialty': 'General dentistry', 'services': ['checkup', 'cleaning', 'whitening']},
    {'id': 'dr-omar', 'name': 'Dr. Omar Hassan', 'specialty': 'Restorative dentistry', 'services': ['checkup', 'implant', 'tooth pain']},
]


class BookingError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def result(message, **data):
    return {'ok': True, 'message': message, **data}


class AppointmentService:
    """Local demo adapter. Transactions and unique indexes protect same-slot writes."""

    def __init__(self, store: Store):
        self.store = store

    def doctor(self, doctor_id):
        doctor = next((d for d in DOCTORS if d['id'] == doctor_id), None)
        if not doctor:
            raise BookingError('UNKNOWN_DOCTOR', 'Please choose one of the clinic doctors.')
        return doctor

    def availability(self, doctor_id, day):
        self.doctor(doctor_id)
        try:
            target = date.fromisoformat(day)
        except ValueError:
            raise BookingError('INVALID_DATE', 'Please give a date in YYYY-MM-DD format.') from None
        now = datetime.now(DUBAI)
        if target < now.date() or target > now.date() + timedelta(days=90):
            raise BookingError('INVALID_DATE', 'Please choose a date within the next 90 days.')
        if target.weekday() == 6:
            return result('The demo clinic is closed on Sunday.', slots=[])
        with self.store.connection() as db:
            busy = {r['start'] for r in db.execute(
                "SELECT start FROM appointments WHERE doctor_id=? AND status='confirmed'", (doctor_id,))}
            held = {r['start'] for r in db.execute(
                'SELECT start FROM holds WHERE doctor_id=? AND expires>?', (doctor_id, time.time()))}
        slots = []
        for half_hour in range(18, 36):
            if half_hour in (26, 27):  # Lunch: 13:00–14:00.
                continue
            start = datetime.combine(target, clock_time(half_hour // 2, (half_hour % 2) * 30), DUBAI)
            canonical = start.astimezone(timezone.utc).isoformat()
            if start > now + timedelta(minutes=30) and canonical not in busy | held:
                slots.append({'start': canonical, 'end': (start + timedelta(minutes=30)).astimezone(timezone.utc).isoformat(),
                              'label': start.strftime('%I:%M %p').lstrip('0')})
        return result('These are demo slots in Dubai time.', doctor=self.doctor(doctor_id), date=day, slots=slots)

    def appointments(self, uid):
        with self.store.connection() as db:
            rows = db.execute("SELECT * FROM appointments WHERE patient_id=? AND status='confirmed' AND start>? ORDER BY start",
                              (uid, datetime.now(timezone.utc).isoformat())).fetchall()
        return result('Your upcoming demo appointments.', appointments=[dict(r) for r in rows])

    def prepare(self, uid, session_id, doctor_id, start, reason, appointment_id=None):
        self.doctor(doctor_id)
        try:
            parsed = datetime.fromisoformat(start)
            if parsed.tzinfo is None:
                raise ValueError()
            start = parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            raise BookingError('INVALID_TIME', 'Choose a timezone-aware slot returned by availability.') from None
        day = parsed.astimezone(DUBAI).date().isoformat()
        # Releasing the session's old hold makes choosing a different slot possible.
        self.release(uid, session_id)
        slots = self.availability(doctor_id, day)['slots']
        slot = next((s for s in slots if s['start'] == start), None)
        if not slot:
            raise BookingError('SLOT_UNAVAILABLE', 'That slot is unavailable. Please choose another time.')
        hold_id = str(uuid.uuid4())
        with self.store.connection(write=True) as db:
            db.execute('DELETE FROM holds WHERE expires<=?', (time.time(),))
            if appointment_id:
                target = db.execute("SELECT id FROM appointments WHERE id=? AND patient_id=? AND status='confirmed'",
                                    (appointment_id, uid)).fetchone()
                if not target:
                    raise BookingError('NOT_FOUND', 'That active appointment was not found.')
            occupied = db.execute("SELECT id FROM appointments WHERE doctor_id=? AND start=? AND status='confirmed'", (doctor_id, start)).fetchone()
            held = db.execute('SELECT id FROM holds WHERE doctor_id=? AND start=?', (doctor_id, start)).fetchone()
            if occupied or held:
                raise BookingError('SLOT_UNAVAILABLE', 'Another patient selected that slot. Please choose another.')
            db.execute('INSERT INTO holds VALUES (?,?,?,?,?,?,?,?,?)',
                       (hold_id, uid, session_id, doctor_id, start, slot['end'], reason[:500], time.time() + 120, appointment_id))
        local = parsed.astimezone(DUBAI).strftime('%A %d %B at %I:%M %p')
        return result(f"{'Move your appointment to' if appointment_id else 'Book'} {self.doctor(doctor_id)['name']} on {local}, Dubai time, for {reason}? Say confirm or no.",
                      hold_id=hold_id, expires_in=120, slot=slot, doctor=self.doctor(doctor_id), requires_confirmation=True)

    def confirm(self, uid, session_id, hold_id):
        key = f'confirm:{hold_id}'
        with self.store.connection(write=True) as db:
            previous = db.execute('SELECT response FROM operations WHERE patient_id=? AND key=?', (uid, key)).fetchone()
            if previous:
                return json.loads(previous['response'])
            hold = db.execute('SELECT * FROM holds WHERE id=? AND patient_id=? AND session_id=?', (hold_id, uid, session_id)).fetchone()
            if not hold or hold['expires'] <= time.time():
                raise BookingError('HOLD_EXPIRED', 'The two-minute hold expired. Please select a slot again.')
            if db.execute("SELECT id FROM appointments WHERE doctor_id=? AND start=? AND status='confirmed'", (hold['doctor_id'], hold['start'])).fetchone():
                raise BookingError('SLOT_UNAVAILABLE', 'The slot is no longer available. Please select another.')
            if hold['appointment_id']:
                changed = db.execute("UPDATE appointments SET doctor_id=?, start=?, end=?, reason=? WHERE id=? AND patient_id=? AND status='confirmed'",
                                     (hold['doctor_id'], hold['start'], hold['end'], hold['reason'], hold['appointment_id'], uid)).rowcount
                if not changed:
                    raise BookingError('NOT_FOUND', 'The original appointment is no longer active.')
                appointment_id = hold['appointment_id']
            else:
                appointment_id = str(uuid.uuid4())
                db.execute('INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?)',
                           (appointment_id, uid, hold['doctor_id'], hold['start'], hold['end'], hold['reason'], 'confirmed', datetime.now(timezone.utc).isoformat()))
            response = result('Your demo appointment is confirmed. This is a local test booking; no actual calendar event or email was sent.', appointment_id=appointment_id)
            db.execute('INSERT INTO operations VALUES (?,?,?)', (uid, key, json.dumps(response)))
            db.execute('DELETE FROM holds WHERE id=?', (hold_id,))
        return response

    def cancel(self, uid, appointment_id):
        with self.store.connection(write=True) as db:
            row = db.execute('SELECT status FROM appointments WHERE id=? AND patient_id=?', (appointment_id, uid)).fetchone()
            if not row:
                raise BookingError('NOT_FOUND', 'That appointment was not found.')
            db.execute("UPDATE appointments SET status='cancelled' WHERE id=? AND patient_id=?", (appointment_id, uid))
        return result('Your demo appointment is cancelled.', appointment_id=appointment_id)

    def release(self, uid, session_id):
        with self.store.connection(write=True) as db:
            db.execute('DELETE FROM holds WHERE patient_id=? AND session_id=?', (uid, session_id))
        return result('The slot hold has been released.')
