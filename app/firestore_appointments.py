"""Guest bookings persisted in Firestore, optionally with clinic Calendar invitations."""
import hashlib
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from .appointments import BookingError, result
from .config import settings
from .real_appointments import GoogleCalendarAppointmentService
from .host_google import HostGoogleOAuth


class FirestoreAppointmentService(GoogleCalendarAppointmentService):
    # Reuse clinic hours and slot formatting, but never initialize or call OAuth.
    def __init__(self):
        try:
            self.admin_app = firebase_admin.get_app('dental-firestore-bookings')
        except ValueError:
            self.admin_app = firebase_admin.initialize_app(
                credentials.Certificate(str(Path(settings.google_application_credentials))),
                {'projectId': settings.firebase_project_id}, name='dental-firestore-bookings')
        self.db = firestore.client(app=self.admin_app, database_id=settings.firestore_database_id)
        self.timezone = ZoneInfo(settings.clinic_timezone)
        self.doctors, self.services, self.default_slot_minutes = self._load_clinic_data()
        self.host_google = HostGoogleOAuth(self.db) if settings.calendar_invitations_enabled else None

    def public_doctors(self):
        if not self.host_google:
            return self.doctors
        return [{**doctor, 'calendar_connected': self.host_google.status(doctor['id'])['connected']}
                for doctor in self.doctors]

    def _busy_periods(self, doctor, start, end, exclude_event_id=None):
        periods = []
        for snapshot in self.db.collection('appointments').where(
                filter=FieldFilter('doctor_id', '==', doctor['id'])).stream():
            item = snapshot.to_dict()
            if item.get('status') != 'confirmed' or snapshot.id == exclude_event_id:
                continue
            begin = datetime.fromisoformat(self._iso_value(item['start'])).astimezone(self.timezone)
            finish = datetime.fromisoformat(self._iso_value(item['end'])).astimezone(self.timezone)
            if begin < end and finish > start:
                periods.append((begin, finish))
        return periods

    def availability(self, doctor_id, day):
        response = super().availability(doctor_id, day)
        response['message'] = 'Available clinic appointments.'
        return response

    @staticmethod
    def contact(name, phone):
        name = ' '.join(str(name or '').split())
        phone = re.sub(r'[\s().-]', '', str(phone or ''))
        if not name or len(name) > 80:
            raise BookingError('INVALID_NAME', 'Please tell me the client name.')
        if not re.fullmatch(r'\+?[0-9]{7,15}', phone):
            raise BookingError('INVALID_PHONE', 'Please tell me a phone number with 7 to 15 digits, including the country code if needed.')
        return name, phone

    @staticmethod
    def invitation_email(email):
        email = str(email or '').strip().lower()
        pattern = r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+"
        if not re.fullmatch(pattern, email, re.IGNORECASE):
            raise BookingError('INVALID_EMAIL', 'Please tell me a valid email address for the calendar invitation.')
        return email

    @staticmethod
    def slot_key(doctor_id, start):
        return hashlib.sha256(f'{doctor_id}:{start}'.encode()).hexdigest()

    def prepare(self, uid, session_id, doctor_id, start, reason='Appointment', appointment_id=None,
                client_name='', phone_number='', client_email=''):
        name, phone = self.contact(client_name, phone_number)
        email = self.invitation_email(client_email) if settings.calendar_invitations_enabled else ''
        doctor = self.doctor(doctor_id)
        try:
            parsed = datetime.fromisoformat(start)
            if parsed.tzinfo is None:
                raise ValueError()
            canonical = parsed.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError):
            raise BookingError('INVALID_TIME', 'Choose a time from the available slots.') from None
        self.release(uid, session_id)
        available = self.availability(doctor_id, parsed.astimezone(self.timezone).date().isoformat())
        slot = next((s for s in available['slots'] if s['start'] == canonical), None)
        if not slot:
            raise BookingError('SLOT_UNAVAILABLE', 'That time is unavailable. Please choose another slot.')
        key = self.slot_key(doctor_id, canonical)
        hold_ref = self.db.collection('slot_holds').document(key)
        slot_ref = self.db.collection('booking_slots').document(key)
        hold_id = str(uuid.uuid4())

        @firestore.transactional
        def reserve(tx):
            occupied = slot_ref.get(transaction=tx)
            held = hold_ref.get(transaction=tx)
            if appointment_id:
                old = self.db.collection('appointments').document(appointment_id).get(transaction=tx)
                if not old.exists or old.to_dict().get('patient_id') != uid or old.to_dict().get('status') != 'confirmed':
                    raise BookingError('NOT_FOUND', 'That active appointment was not found.')
            if occupied.exists or (held.exists and held.to_dict().get('expires_at', 0) > time.time()):
                raise BookingError('SLOT_UNAVAILABLE', 'That time was just selected. Please choose another slot.')
            tx.set(hold_ref, {'id': hold_id, 'patient_id': uid, 'session_id': session_id,
                             'doctor_id': doctor_id, 'start': slot['start'], 'end': slot['end'],
                             'reason': reason[:500], 'client_name': name, 'phone_number': phone,
                             'client_email': email,
                             'appointment_id': appointment_id, 'expires_at': time.time() + 120})
        reserve(self.db.transaction())
        summary = ('Read back the name, phone number, email, date and time, then ask for confirmation.'
                   if settings.calendar_invitations_enabled else
                   'Read back the name, phone number, date and time, then ask for confirmation.')
        return result(summary,
                      hold_id=key + ':' + hold_id, slot=slot, doctor=doctor, client_name=name,
                      phone_number=phone, requires_confirmation=True, expires_in=120)

    def confirm(self, uid, session_id, hold_id):
        key, _, nonce = hold_id.partition(':')
        hold_ref = self.db.collection('slot_holds').document(key)
        slot_ref = self.db.collection('booking_slots').document(key)
        operation_key = hashlib.sha256(f'{uid}:{session_id}:{hold_id}'.encode()).hexdigest()
        operation_ref = self.db.collection('booking_operations').document(operation_key)
        new_id = str(uuid.uuid4())

        @firestore.transactional
        def commit(tx):
            previous = operation_ref.get(transaction=tx)
            if previous.exists:
                return previous.to_dict()['response']
            snapshot = hold_ref.get(transaction=tx)
            hold = snapshot.to_dict() if snapshot.exists else {}
            if (hold.get('id') != nonce or hold.get('patient_id') != uid or
                    hold.get('session_id') != session_id or hold.get('expires_at', 0) <= time.time()):
                raise BookingError('HOLD_EXPIRED', 'The hold expired. Please select a time again.')
            occupied = slot_ref.get(transaction=tx)
            if occupied.exists:
                raise BookingError('SLOT_UNAVAILABLE', 'That time is no longer available.')
            appointment_id = hold.get('appointment_id') or new_id
            appointment_ref = self.db.collection('appointments').document(appointment_id)
            old = appointment_ref.get(transaction=tx)
            if hold.get('appointment_id') and (not old.exists or old.to_dict().get('patient_id') != uid or old.to_dict().get('status') != 'confirmed'):
                raise BookingError('NOT_FOUND', 'That active appointment was not found.')
            old_slot_ref = None
            if old.exists and old.to_dict().get('booking_slot_key'):
                old_slot_ref = self.db.collection('booking_slots').document(old.to_dict()['booking_slot_key'])
                old_slot = old_slot_ref.get(transaction=tx)
                if not old_slot.exists or old_slot.to_dict().get('appointment_id') != appointment_id:
                    raise BookingError('SLOT_UNAVAILABLE', 'The original appointment changed. Please check your appointments.')
            start = datetime.fromisoformat(hold['start'])
            appointment = {'patient_id': uid, 'doctor_id': hold['doctor_id'],
                           'client_name': hold['client_name'], 'phone_number': hold['phone_number'],
                           'client_email': hold.get('client_email', ''),
                           'start': start, 'end': datetime.fromisoformat(hold['end']),
                           'appointment_date': start.astimezone(self.timezone).date().isoformat(),
                           'appointment_time': start.astimezone(self.timezone).strftime('%H:%M'),
                           'timezone': str(self.timezone), 'reason': hold['reason'], 'status': 'confirmed',
                           'booking_slot_key': key, 'booking_source': 'firestore',
                           'updated_at': firestore.SERVER_TIMESTAMP}
            if not old.exists:
                appointment['created_at'] = firestore.SERVER_TIMESTAMP
            response = result('Your appointment is booked.', appointment_id=appointment_id,
                              client_name=hold['client_name'], phone_number=hold['phone_number'],
                              start=hold['start'], end=hold['end'], customer_invited=False)
            tx.set(appointment_ref, appointment, merge=True)
            tx.set(slot_ref, {'appointment_id': appointment_id})
            if old_slot_ref:
                tx.delete(old_slot_ref)
            tx.delete(hold_ref)
            tx.set(operation_ref, {'response': response})
            return response
        response = commit(self.db.transaction())
        self._send_calendar_invitation(uid, response['appointment_id'])
        saved = self.db.collection('appointments').document(response['appointment_id']).get().to_dict()
        response['customer_invited'] = bool(saved and saved.get('calendar_invitation_status') == 'sent')
        response['calendar_link'] = saved.get('calendar_html_link', '') if saved else ''
        response['message'] = ('Your appointment is booked. Google Calendar sent the invitation to your email.'
                               if response['customer_invited'] else
                               'Your appointment is booked and saved. The calendar invitation could not be verified; contact the clinic if it does not arrive.')
        operation_key = hashlib.sha256(f'{uid}:{session_id}:{hold_id}'.encode()).hexdigest()
        self.db.collection('booking_operations').document(operation_key).set({'response': response}, merge=True)
        return response

    def _send_calendar_invitation(self, uid, appointment_id):
        """Create one Calendar event; Google sends its RSVP invitation to the patient."""
        if not settings.calendar_invitations_enabled:
            return
        ref = self.db.collection('appointments').document(appointment_id)
        snapshot = ref.get()
        if not snapshot.exists:
            return
        appointment = snapshot.to_dict()
        if appointment.get('patient_id') != uid or appointment.get('status') != 'confirmed':
            return
        if appointment.get('calendar_invitation_status') == 'sent':
            return
        try:
            doctor = self.doctor(appointment['doctor_id'])
            self.host_google.access_token(doctor['id'])
            customer = {'id': uid, 'name': appointment['client_name'], 'email': appointment['client_email']}
            hold = {'reason': appointment['reason'], 'start': appointment['start'], 'end': appointment['end']}
            event_id = appointment.get('google_calendar_event_id')
            if event_id:
                event = self._calendar_request(doctor, 'PATCH', '/' + event_id,
                    params=self._calendar_params(), json=self._event_payload(appointment_id, doctor, hold, customer)).json()
            else:
                payload = self._event_payload(appointment_id, doctor, hold, customer, include_id=True)
                try:
                    event = self._calendar_request(doctor, 'POST', params=self._calendar_params(), json=payload).json()
                except BookingError as exc:
                    if exc.code != 'CALENDAR_CONFLICT':
                        raise
                    event = self._calendar_request(doctor, 'GET', '/' + payload['id']).json()
                event_id = event['id']
            ref.update({'google_calendar_event_id': event_id,
                        'calendar_ical_uid': event.get('iCalUID', ''),
                        'calendar_html_link': event.get('htmlLink', ''),
                        'calendar_invitation_status': 'sent',
                        'updated_at': firestore.SERVER_TIMESTAMP})
        except Exception as exc:
            ref.update({'calendar_invitation_status': 'failed',
                        'calendar_invitation_error': str(getattr(exc, 'code', 'CALENDAR_INVITATION_FAILED')),
                        'updated_at': firestore.SERVER_TIMESTAMP})

    def cancel(self, uid, appointment_id):
        ref = self.db.collection('appointments').document(appointment_id)

        @firestore.transactional
        def remove(tx):
            snapshot = ref.get(transaction=tx)
            item = snapshot.to_dict() if snapshot.exists else {}
            if item.get('patient_id') != uid:
                raise BookingError('NOT_FOUND', 'That appointment was not found.')
            if item.get('status') == 'cancelled':
                return result('That appointment is already cancelled.', appointment_id=appointment_id)
            slot_ref = None
            if item.get('booking_slot_key'):
                slot_ref = self.db.collection('booking_slots').document(item['booking_slot_key'])
                slot = slot_ref.get(transaction=tx)
                if not slot.exists or slot.to_dict().get('appointment_id') != appointment_id:
                    slot_ref = None
            tx.update(ref, {'status': 'cancelled', 'updated_at': firestore.SERVER_TIMESTAMP})
            if slot_ref:
                tx.delete(slot_ref)
            return result('Your appointment is cancelled.', appointment_id=appointment_id)
        return remove(self.db.transaction())
