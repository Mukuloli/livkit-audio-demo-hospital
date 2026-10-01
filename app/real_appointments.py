"""Firestore bookings managed through each host's Google OAuth connection."""
import base64
import hashlib
import json
import logging
import time
import uuid
from datetime import date, datetime, time as clock_time, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import firebase_admin
import requests
from firebase_admin import auth, credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from .appointments import BookingError, result
from .config import BASE_DIR, settings
from .host_google import HostGoogleOAuth


class GoogleCalendarAppointmentService:
    """Owns availability and writes appointments as the connected doctor."""

    def __init__(self):
        credential_path = Path(settings.google_application_credentials)
        if not credential_path.is_file():
            raise ValueError('Firebase service-account JSON was not found.')
        try:
            self.admin_app = firebase_admin.get_app('dental-real-bookings')
        except ValueError:
            self.admin_app = firebase_admin.initialize_app(
                credentials.Certificate(str(credential_path)),
                {'projectId': settings.firebase_project_id},
                name='dental-real-bookings',
            )
        self.db = firestore.client(app=self.admin_app, database_id=settings.firestore_database_id)
        self.host_google = HostGoogleOAuth(self.db)
        self.timezone = ZoneInfo(settings.clinic_timezone)
        self.doctors, self.services, self.default_slot_minutes = self._load_clinic_data()

    def _load_clinic_data(self):
        if settings.clinic_data_path:
            path = Path(settings.clinic_data_path)
            if not path.is_absolute():
                path = BASE_DIR.parent / path
            data = json.loads(path.read_text(encoding='utf-8'))
            if 'REPLACE_' in json.dumps(data):
                raise ValueError('CLINIC_DATA_PATH still contains REPLACE_ placeholders.')
            clinic = data['clinic']
            self.timezone = ZoneInfo(clinic.get('timezone', settings.clinic_timezone))
            doctors = [self._normalize_doctor(item) for item in data['doctors'] if item.get('active', True)]
            services = {item['service_id']: item for item in data.get('services', []) if item.get('active', True)}
            return doctors, services, int(clinic.get('default_slot_minutes', 30))
        doctor = self._normalize_doctor({
            'doctor_id': settings.doctor_id,
            'name': settings.doctor_name,
            'specialization': settings.doctor_specialty,
            'services': [value.strip() for value in settings.doctor_services.split(',') if value.strip()],
            'active': True,
        })
        return [doctor], {}, 30

    @staticmethod
    def _normalize_doctor(item):
        return {
            'id': item['doctor_id'],
            'name': item['name'],
            'specialty': item.get('specialization', ''),
            'services': item.get('services', []),
            'working_hours': item.get('working_hours'),
            'recurring_breaks': item.get('recurring_breaks', []),
            'leave': item.get('leave', []),
            'languages': item.get('languages', []),
            'photo_url': item.get('photo_url'),
            'location': item.get('location') or settings.appointment_location,
        }

    def public_doctors(self):
        rows = []
        for doctor in self.doctors:
            status = self.host_google.status(doctor['id'])
            rows.append({**doctor, 'calendar_connected': status['connected']})
        return rows

    def doctor(self, doctor_id):
        doctor = next((item for item in self.doctors if item['id'] == doctor_id), None)
        if not doctor:
            raise BookingError('UNKNOWN_DOCTOR', 'Please choose one of the available doctors.')
        return doctor

    @staticmethod
    def _iso_value(value):
        return value.isoformat() if isinstance(value, datetime) else value

    def _google_request(self, token, method, url, **kwargs):
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json', **kwargs.pop('headers', {})}
        try:
            response = requests.request(method, url, headers=headers, timeout=20, **kwargs)
        except requests.RequestException:
            raise BookingError('GOOGLE_API_UNAVAILABLE', 'Google Calendar is temporarily unavailable. Please retry.') from None
        if response.status_code in {401, 403}:
            raise BookingError('HOST_CALENDAR_ACCESS_DENIED', 'The doctor must reconnect Google Calendar.')
        if response.status_code == 404:
            raise BookingError('CALENDAR_NOT_FOUND', 'The connected Google Calendar event was not found.')
        if response.status_code == 409:
            raise BookingError('CALENDAR_CONFLICT', 'This Google Calendar event already exists.')
        if not response.ok:
            raise BookingError('GOOGLE_API_ERROR', f'Google returned HTTP {response.status_code}. Please retry.')
        return response

    def _calendar_request(self, doctor, method, suffix='', **kwargs):
        token, connection = self.host_google.access_token(doctor['id'])
        calendar_id = connection.get('calendar_id', 'primary')
        url = 'https://www.googleapis.com/calendar/v3/calendars/' + quote(calendar_id, safe='') + '/events' + suffix
        return self._google_request(token, method, url, **kwargs)

    def _busy_periods(self, doctor, start, end, exclude_event_id=None):
        response = self._calendar_request(doctor, 'GET', params={
            'timeMin': start.isoformat(), 'timeMax': end.isoformat(), 'singleEvents': 'true',
            'orderBy': 'startTime', 'maxResults': 250, 'timeZone': str(self.timezone),
        }).json()
        periods = []
        for event in response.get('items', []):
            if event.get('id') == exclude_event_id or event.get('status') == 'cancelled' or event.get('transparency') == 'transparent':
                continue
            periods.append((self._event_time(event.get('start', {}), start), self._event_time(event.get('end', {}), end)))
        return periods

    def _event_time(self, value, boundary):
        if value.get('dateTime'):
            return datetime.fromisoformat(value['dateTime'].replace('Z', '+00:00')).astimezone(self.timezone)
        if value.get('date'):
            return datetime.combine(date.fromisoformat(value['date']), clock_time.min, self.timezone)
        return boundary

    @staticmethod
    def _overlaps(start, end, periods):
        return any(start < busy_end and end > busy_start for busy_start, busy_end in periods)

    def _working_periods(self, doctor, target):
        day_name = target.strftime('%A').lower()
        configured = doctor.get('working_hours')
        ranges = configured.get(day_name, []) if configured else ([] if target.weekday() == 6 else [{'start': '09:00', 'end': '18:00'}])
        return [(datetime.combine(target, clock_time.fromisoformat(item['start']), self.timezone),
                 datetime.combine(target, clock_time.fromisoformat(item['end']), self.timezone)) for item in ranges]

    def _blocked_periods(self, doctor, target, day_start, day_end, exclude_event_id=None):
        periods = self._busy_periods(doctor, day_start, day_end, exclude_event_id)
        day_name = target.strftime('%A').lower()
        for item in doctor.get('recurring_breaks', []):
            if day_name in item.get('days', []):
                periods.append((datetime.combine(target, clock_time.fromisoformat(item['start']), self.timezone),
                                datetime.combine(target, clock_time.fromisoformat(item['end']), self.timezone)))
        for item in doctor.get('leave', []):
            leave_start = datetime.fromisoformat(item['start']).astimezone(self.timezone)
            leave_end = datetime.fromisoformat(item['end']).astimezone(self.timezone)
            if leave_start < day_end and leave_end > day_start:
                periods.append((leave_start, leave_end))
        now = time.time()
        query = self.db.collection('slot_holds').where(filter=FieldFilter('doctor_id', '==', doctor['id']))
        for snapshot in query.stream():
            hold = snapshot.to_dict()
            if hold.get('expires_at', 0) <= now:
                snapshot.reference.delete()
                continue
            periods.append((datetime.fromisoformat(hold['start']).astimezone(self.timezone),
                            datetime.fromisoformat(hold['end']).astimezone(self.timezone)))
        return periods

    def availability(self, doctor_id, day):
        doctor = self.doctor(doctor_id)
        try:
            target = date.fromisoformat(day)
        except ValueError:
            raise BookingError('INVALID_DATE', 'Please give a date in YYYY-MM-DD format.') from None
        now = datetime.now(self.timezone)
        if target < now.date() or target > now.date() + timedelta(days=90):
            raise BookingError('INVALID_DATE', 'Please choose a date within the next 90 days.')
        working = self._working_periods(doctor, target)
        if not working:
            return result(f"{doctor['name']} is not working that day.", doctor=doctor, date=day, slots=[])
        day_start = datetime.combine(target, clock_time.min, self.timezone)
        blocked = self._blocked_periods(doctor, target, day_start, day_start + timedelta(days=1))
        duration = timedelta(minutes=self.default_slot_minutes)
        slots = []
        for period_start, period_end in working:
            cursor = period_start
            while cursor + duration <= period_end:
                slot_end = cursor + duration
                if cursor > now + timedelta(minutes=30) and not self._overlaps(cursor, slot_end, blocked):
                    slots.append({'start': cursor.astimezone(timezone.utc).isoformat(),
                                  'end': slot_end.astimezone(timezone.utc).isoformat(),
                                  'label': cursor.strftime('%I:%M %p').lstrip('0')})
                cursor = slot_end
        return result('Live availability from the doctor Google Calendar.', doctor=doctor, date=day, slots=slots)

    def appointments(self, uid):
        query = self.db.collection('appointments').where(filter=FieldFilter('patient_id', '==', uid))
        appointments = []
        now = datetime.now(timezone.utc)
        for snapshot in query.stream():
            item = snapshot.to_dict()
            if item.get('status') != 'confirmed':
                continue
            for field in ('start', 'end'):
                if isinstance(item.get(field), datetime):
                    item[field] = item[field].isoformat()
            if datetime.fromisoformat(item['start']).astimezone(timezone.utc) <= now:
                continue
            item['id'] = snapshot.id
            appointments.append(item)
        appointments.sort(key=lambda item: item['start'])
        return result('Your upcoming confirmed appointments from Firestore.', appointments=appointments)

    def prepare(self, uid, session_id, doctor_id, start, reason, appointment_id=None):
        doctor = self.doctor(doctor_id)
        self.host_google.connection(doctor_id)
        try:
            parsed = datetime.fromisoformat(start)
            if parsed.tzinfo is None:
                raise ValueError()
            canonical = parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            raise BookingError('INVALID_TIME', 'Choose a timezone-aware slot returned by availability.') from None
        if appointment_id:
            appointment = self.db.collection('appointments').document(appointment_id).get()
            if not appointment.exists or appointment.to_dict().get('patient_id') != uid or appointment.to_dict().get('status') != 'confirmed':
                raise BookingError('NOT_FOUND', 'That active appointment was not found.')
        self.release(uid, session_id)
        slots = self.availability(doctor_id, parsed.astimezone(self.timezone).date().isoformat())['slots']
        slot = next((item for item in slots if item['start'] == canonical), None)
        if not slot:
            raise BookingError('SLOT_UNAVAILABLE', 'That time is busy on the doctor Google Calendar.')
        hold_key = hashlib.sha256(f'{doctor_id}:{canonical}'.encode()).hexdigest()
        hold_ref = self.db.collection('slot_holds').document(hold_key)
        transaction = self.db.transaction()

        @firestore.transactional
        def reserve(tx):
            existing = hold_ref.get(transaction=tx)
            if existing.exists and existing.to_dict().get('expires_at', 0) > time.time():
                raise BookingError('SLOT_UNAVAILABLE', 'Another customer is currently selecting that time.')
            tx.set(hold_ref, {'id': hold_key, 'patient_id': uid, 'session_id': session_id, 'doctor_id': doctor_id,
                              'start': slot['start'], 'end': slot['end'], 'reason': reason[:500],
                              'expires_at': time.time() + 120, 'appointment_id': appointment_id})

        reserve(transaction)
        local = parsed.astimezone(self.timezone).strftime('%A %d %B at %I:%M %p')
        action = 'Move your appointment to' if appointment_id else 'Book'
        return result(f"{action} {doctor['name']} on {local}, {self.timezone.key} time, for {reason}? Say confirm or no.",
                      hold_id=hold_key, expires_in=120, slot=slot, doctor=doctor, requires_confirmation=True)

    def _customer(self, uid):
        try:
            record = auth.get_user(uid, app=self.admin_app)
        except Exception:
            raise BookingError('CUSTOMER_PROFILE_ERROR', 'The signed-in customer profile could not be read.') from None
        if not record.email:
            raise BookingError('CUSTOMER_EMAIL_REQUIRED', 'Add an email address before booking this appointment.')
        return {'id': uid, 'name': record.display_name or record.email.split('@')[0], 'email': record.email}

    def _event_payload(self, appointment_id, doctor, hold, customer, include_id=False):
        payload = {
            'summary': f"{doctor['name']} - {hold['reason']}",
            'description': f"Booked automatically by Noor. Appointment ID: {appointment_id}",
            'start': {'dateTime': self._iso_value(hold['start']), 'timeZone': str(self.timezone)},
            'end': {'dateTime': self._iso_value(hold['end']), 'timeZone': str(self.timezone)},
            'attendees': [{'email': customer['email'], 'displayName': customer['name']}],
            'guestsCanInviteOthers': False, 'guestsCanModify': False, 'transparency': 'opaque',
            'extendedProperties': {'private': {'noorAppointmentId': appointment_id,
                                                'firebaseUid': customer['id'], 'hostId': doctor['id']}},
        }
        if include_id:
            payload['id'] = 'noor' + hashlib.sha256(appointment_id.encode()).hexdigest()[:40]
        if doctor.get('location'):
            payload['location'] = doctor['location']
        if settings.google_meet_enabled:
            payload['conferenceData'] = {'createRequest': {'requestId': 'meet-' + appointment_id,
                                                            'conferenceSolutionKey': {'type': 'hangoutsMeet'}}}
        return payload

    @staticmethod
    def _meeting_link(event):
        if event.get('hangoutLink'):
            return event['hangoutLink']
        for entry in event.get('conferenceData', {}).get('entryPoints', []):
            if entry.get('entryPointType') == 'video':
                return entry.get('uri', '')
        return ''

    def _calendar_params(self):
        params = {'sendUpdates': 'all'}
        if settings.google_meet_enabled:
            params['conferenceDataVersion'] = 1
        return params

    def _send_confirmation_email(self, doctor, connection, customer, appointment):
        local_start = appointment['start'].astimezone(self.timezone)
        local_end = appointment['end'].astimezone(self.timezone)
        message = EmailMessage()
        message['From'] = connection['google_email']
        message['To'] = customer['email']
        message['Subject'] = 'Your clinic appointment is confirmed'
        details = [f"Hello {customer['name']},", '', 'Your clinic appointment is confirmed.',
                   f"Service: {appointment['reason']}", f"Date: {local_start.strftime('%A, %d %B %Y')}",
                   f"Time: {local_start.strftime('%I:%M %p')} - {local_end.strftime('%I:%M %p')} ({self.timezone.key})"]
        if appointment.get('location'):
            details.append('Location: ' + appointment['location'])
        if appointment.get('meeting_link'):
            details.append('Meeting: ' + appointment['meeting_link'])
        details.extend(['', 'Your calendar invitation is attached. Google Calendar also sends an invitation.', '', 'Noor appointment assistant'])
        message.set_content('\n'.join(details))
        message.add_attachment(self._calendar_attachment(connection, customer, appointment),
                               subtype='calendar', charset='utf-8',
                               params={'method': 'REQUEST'}, filename='appointment.ics')
        token, _ = self.host_google.access_token(doctor['id'])
        response = self._google_request(token, 'POST', 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
                                        json={'raw': base64.urlsafe_b64encode(message.as_bytes()).decode()}).json()
        return response.get('id', '')

    @staticmethod
    def _calendar_attachment(connection, customer, appointment):
        def escape(value):
            return str(value).replace('\\', '\\\\').replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')

        def stamp(value):
            return value.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')

        lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Noor//Clinic appointments//EN',
                 'CALSCALE:GREGORIAN', 'METHOD:REQUEST', 'BEGIN:VEVENT',
                 'UID:' + escape(appointment['calendar_ical_uid']),
                 'DTSTAMP:' + stamp(datetime.now(timezone.utc)),
                 'DTSTART:' + stamp(appointment['start']), 'DTEND:' + stamp(appointment['end']),
                 'SEQUENCE:' + str(appointment.get('calendar_sequence', 0)),
                 'SUMMARY:Clinic appointment', 'DESCRIPTION:' + escape(appointment['reason']),
                 'ORGANIZER:mailto:' + escape(connection['google_email']),
                 'ATTENDEE;RSVP=TRUE:mailto:' + escape(customer['email']), 'STATUS:CONFIRMED']
        if appointment.get('location'):
            lines.append('LOCATION:' + escape(appointment['location']))
        if appointment.get('meeting_link'):
            lines.append('URL:' + escape(appointment['meeting_link']))
        lines.extend(['END:VEVENT', 'END:VCALENDAR'])
        # RFC 5545: fold at 75 octets, never inside a UTF-8 character.
        folded = []
        for line in lines:
            chunk = ''
            for char in line:
                if len((chunk + char).encode('utf-8')) > 75:
                    folded.append(chunk)
                    chunk = ' '
                chunk += char
            folded.append(chunk)
        return '\r\n'.join(folded) + '\r\n'

    def _rollback_event(self, doctor, event_id):
        try:
            self._calendar_request(doctor, 'DELETE', '/' + quote(event_id, safe=''), params={'sendUpdates': 'all'})
        except BookingError:
            pass

    def confirm(self, uid, session_id, hold_id):
        operation_key = hashlib.sha256(f'{hold_id}:{session_id}'.encode()).hexdigest()
        operation_ref = self.db.collection('booking_operations').document(f'confirm-{operation_key}')
        previous = operation_ref.get()
        if previous.exists:
            return previous.to_dict()['response']
        hold_ref = self.db.collection('slot_holds').document(hold_id)
        hold_snapshot = hold_ref.get()
        if not hold_snapshot.exists:
            raise BookingError('HOLD_EXPIRED', 'The two-minute hold expired. Please select a slot again.')
        hold = hold_snapshot.to_dict()
        if hold.get('patient_id') != uid or hold.get('session_id') != session_id or hold.get('expires_at', 0) <= time.time():
            raise BookingError('HOLD_EXPIRED', 'The two-minute hold expired. Please select a slot again.')
        doctor = self.doctor(hold['doctor_id'])
        customer = self._customer(uid)
        appointment_id = hold.get('appointment_id') or str(uuid.uuid4())
        appointment_ref = self.db.collection('appointments').document(appointment_id)
        existing_data = None
        event_id = None
        if hold.get('appointment_id'):
            existing = appointment_ref.get()
            if not existing.exists or existing.to_dict().get('patient_id') != uid:
                raise BookingError('NOT_FOUND', 'The original appointment is no longer active.')
            existing_data = existing.to_dict()
            if not existing_data.get('host_oauth_managed'):
                raise BookingError('LEGACY_BOOKING', 'This older booking must be migrated before it can be rescheduled.')
            event_id = existing_data.get('google_calendar_event_id') or existing_data.get('calendar_event_id')
        start = datetime.fromisoformat(hold['start']).astimezone(self.timezone)
        end = datetime.fromisoformat(hold['end']).astimezone(self.timezone)
        if self._overlaps(start, end, self._busy_periods(doctor, start, end, event_id)):
            raise BookingError('SLOT_UNAVAILABLE', 'That time became busy on Google Calendar. Please choose another.')
        payload = self._event_payload(appointment_id, doctor, hold, customer, include_id=not event_id)
        created_event = not event_id
        if event_id:
            event = self._calendar_request(doctor, 'PATCH', '/' + quote(event_id, safe=''),
                                           params=self._calendar_params(), json=payload).json()
        else:
            try:
                event = self._calendar_request(doctor, 'POST', params=self._calendar_params(), json=payload).json()
            except BookingError as exc:
                if exc.code != 'CALENDAR_CONFLICT':
                    raise
                event_id = payload['id']
                event = self._calendar_request(doctor, 'GET', '/' + quote(event_id, safe='')).json()
                created_event = False
            event_id = event['id']
        _, connection = self.host_google.access_token(doctor['id'])
        appointment = {
            'patient_id': uid, 'customer_id': uid, 'customer_name': customer['name'],
            'customer_email': customer['email'], 'doctor_id': doctor['id'], 'host_id': doctor['id'],
            'start': datetime.fromisoformat(hold['start']), 'end': datetime.fromisoformat(hold['end']),
            'reason': hold['reason'], 'status': 'confirmed', 'host_oauth_managed': True,
            'google_calendar_id': connection.get('calendar_id', 'primary'),
            'google_calendar_event_id': event_id, 'calendar_event_id': event_id,
            'calendar_ical_uid': event.get('iCalUID') or event_id + '@google.com',
            'calendar_sequence': event.get('sequence', 0),
            'calendar_html_link': event.get('htmlLink', ''), 'meeting_link': self._meeting_link(event),
            'location': doctor.get('location', ''),
            'confirmation_email_status': 'pending' if settings.confirmation_email_enabled else 'disabled',
            'updated_at': firestore.SERVER_TIMESTAMP,
        }
        if not existing_data:
            appointment['created_at'] = firestore.SERVER_TIMESTAMP
        response = result('Your appointment is confirmed. A calendar invitation has been requested for your email.',
                          appointment_id=appointment_id, calendar_event_created=True, calendar_event_id=event_id,
                          calendar_link=event.get('htmlLink', ''), meeting_link=appointment['meeting_link'],
                          customer_invited=True, confirmation_email_sent=False)
        try:
            batch = self.db.batch()
            batch.set(appointment_ref, appointment, merge=True)
            batch.set(operation_ref, {'patient_id': uid, 'response': response, 'created_at': firestore.SERVER_TIMESTAMP})
            batch.delete(hold_ref)
            if settings.confirmation_email_enabled:
                batch.set(self.db.collection('booking_email_outbox').document(appointment_id),
                          {'appointment_id': appointment_id, 'host_id': doctor['id'], 'to': customer['email'],
                           'status': 'pending', 'updated_at': firestore.SERVER_TIMESTAMP}, merge=True)
            batch.commit()
        except Exception:
            if created_event:
                self._rollback_event(doctor, event_id)
            elif existing_data:
                restore_hold = {'start': existing_data['start'], 'end': existing_data['end'],
                                'reason': existing_data['reason']}
                try:
                    self._calendar_request(
                        doctor, 'PATCH', '/' + quote(event_id, safe=''), params=self._calendar_params(),
                        json=self._event_payload(appointment_id, doctor, restore_hold, customer),
                    )
                except BookingError:
                    pass
            raise BookingError('FIRESTORE_ERROR', 'The booking could not be saved; the new calendar event was rolled back.') from None
        if settings.confirmation_email_enabled:
            outbox_ref = self.db.collection('booking_email_outbox').document(appointment_id)
            try:
                gmail_message_id = self._send_confirmation_email(doctor, connection, customer, appointment)
                response['confirmation_email_sent'] = True
                appointment_ref.update({'confirmation_email_status': 'sent',
                                        'confirmation_email_message_id': gmail_message_id,
                                        'updated_at': firestore.SERVER_TIMESTAMP})
                outbox_ref.set({'status': 'sent', 'gmail_message_id': gmail_message_id,
                                'updated_at': firestore.SERVER_TIMESTAMP}, merge=True)
                operation_ref.update({'response': response})
            except Exception as exc:
                # The booking is already committed. Never report it as a failed booking
                # or roll it back because email delivery/status persistence failed.
                from .handoff import request_handoff
                code = getattr(exc, 'code', 'EMAIL_STATUS_UNVERIFIED')
                handoff = request_handoff(None, self, uid, session_id, code)
                response['handoff_requested'] = handoff['handoff_requested']
                response['message'] = ('Your appointment is confirmed, but the confirmation email could not be verified. '
                                       + ('The clinic team has been asked to help.' if handoff['handoff_requested']
                                          else 'Please contact reception for your appointment details.'))
                try:
                    appointment_ref.update({'confirmation_email_status': 'unverified', 'confirmation_email_error': code,
                                            'updated_at': firestore.SERVER_TIMESTAMP})
                    outbox_ref.set({'status': 'unverified', 'error': code,
                                    'updated_at': firestore.SERVER_TIMESTAMP}, merge=True)
                    operation_ref.update({'response': response})
                except Exception:
                    logging.getLogger(__name__).exception('Could not save email follow-up status')
        return response

    def cancel(self, uid, appointment_id):
        appointment_ref = self.db.collection('appointments').document(appointment_id)
        snapshot = appointment_ref.get()
        if not snapshot.exists or snapshot.to_dict().get('patient_id') != uid:
            raise BookingError('NOT_FOUND', 'That appointment was not found.')
        appointment = snapshot.to_dict()
        if appointment.get('status') == 'cancelled':
            return result('That appointment is already cancelled.', appointment_id=appointment_id)
        if not appointment.get('host_oauth_managed'):
            raise BookingError('LEGACY_BOOKING', 'This older booking must be migrated before it can be cancelled here.')
        doctor = self.doctor(appointment['doctor_id'])
        event_id = appointment.get('google_calendar_event_id') or appointment.get('calendar_event_id')
        if event_id:
            self._calendar_request(doctor, 'DELETE', '/' + quote(event_id, safe=''), params={'sendUpdates': 'all'})
        appointment_ref.update({'status': 'cancelled', 'updated_at': firestore.SERVER_TIMESTAMP})
        return result('Appointment cancelled. Google Calendar notified the customer.', appointment_id=appointment_id)

    def release(self, uid, session_id):
        query = self.db.collection('slot_holds').where(filter=FieldFilter('session_id', '==', session_id))
        batch = self.db.batch()
        found = False
        for snapshot in query.stream():
            if snapshot.to_dict().get('patient_id') == uid:
                batch.delete(snapshot.reference)
                found = True
        if found:
            batch.commit()
        return result('The slot hold has been released.')
