import re
from datetime import datetime, timedelta

from .appointments import AppointmentService, BookingError, DOCTORS, DUBAI, result
from .store import Store
from .config import settings


def resolve_day(text):
    today = datetime.now(DUBAI).date()
    if 'tomorrow' in text or 'kal' in text:
        return (today + timedelta(days=1)).isoformat()
    if 'today' in text or 'aaj' in text:
        return today.isoformat()
    match = re.search(r'\b\d{4}-\d{2}-\d{2}\b', text)
    if match:
        return match.group()
    # Explicit weekday resolves to its next occurrence; ambiguous 'next' is clarified.
    if 'next' not in text:
        for i, day in enumerate(['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']):
            if day in text:
                return (today + timedelta(days=(i - today.weekday()) % 7 or 7)).isoformat()
    return None


class VoiceDialog:
    def __init__(self, store: Store, service: AppointmentService):
        self.store, self.service = store, service

    def turn(self, uid, session_id, transcript, client_email=''):
        state = self.store.get_session(session_id, uid)
        if state is None:
            raise BookingError('SESSION_NOT_FOUND', 'Start a new voice session.')
        text = transcript.strip().lower()
        try:
            response = self.advance(uid, session_id, state, text, client_email)
        except BookingError as exc:
            from .handoff import request_handoff
            response = request_handoff(self.store, self.service, uid, session_id, exc.code)
        self.store.save_session(session_id, uid, state)
        return {**response, 'stage': state.get('stage', 'idle')}

    def advance(self, uid, sid, state, text, client_email=''):
        affirmative = bool(re.fullmatch(r'(yes|yes please|confirm|confirm it|confirmed|haan|ha|ji|okay|ok)[.! ]*', text))
        negative = bool(re.fullmatch(r'(no|no thanks|nahi|stop|never mind)[.! ]*', text))
        if negative:
            state.clear()
            self.service.release(uid, sid)
            return result('Okay, I have stopped that request. What else can I help with?')
        if state.get('stage') == 'confirmation':
            if not affirmative:
                return result('Please say confirm to book this exact slot, or no to release it.')
            response = self.service.confirm(uid, sid, state['hold_id'])
            state.clear()
            return response
        if state.get('stage') == 'cancel_confirmation':
            if not affirmative:
                return result('Say confirm to cancel that appointment, or no to keep it.')
            response = self.service.cancel(uid, state['appointment_id'])
            state.clear()
            return response
        if state.get('stage') == 'choose_appointment':
            index = self.selection(text, len(state['appointments']))
            if index is None:
                return result('Please say first, second, or the appointment number.')
            target = state['appointments'][index]
            state['appointment_id'] = target['id']
            return self.modify_target(state, target)
        if not state.get('stage'):
            if any(word in text for word in ['cancel', 'reschedule', 'change', 'my appointment', 'my booking', 'mera appointment']):
                appointments = self.service.appointments(uid)['appointments']
                if not appointments:
                    return result('You have no upcoming demo appointments. Would you like to book one?')
                action = 'cancel' if 'cancel' in text else 'reschedule' if ('reschedule' in text or 'change' in text) else 'list'
                summaries = [self.summary(a) for a in appointments]
                if action == 'list':
                    return result('Your appointments: ' + '; '.join(summaries), appointments=appointments)
                state.update(action=action, appointments=appointments)
                if len(appointments) > 1:
                    state['stage'] = 'choose_appointment'
                    return result('Which appointment? ' + '; '.join(f'{i+1}: {s}' for i, s in enumerate(summaries)))
                state['appointment_id'] = appointments[0]['id']
                return self.modify_target(state, appointments[0])
            if not any(word in text for word in ['book', 'appointment', 'clean', 'check', 'pain', 'whitening', 'implant']):
                return result('I can book, view, reschedule, or cancel a demo dental appointment. Try saying book an appointment.')
            state.update(stage='service', action='book')
        if state['stage'] == 'service':
            reason = next((s for s in ['cleaning', 'whitening', 'implant', 'tooth pain', 'checkup'] if s in text), None)
            if not reason and 'check' in text:
                reason = 'checkup'
            if not reason:
                return result('What would you like: a checkup, cleaning, whitening, implant consultation, or help with tooth pain?')
            doctors = self.service.public_doctors() if hasattr(self.service, 'public_doctors') else DOCTORS
            doctor = next((d for d in doctors if reason in d['services']), None)
            if not doctor:
                raise BookingError('SERVICE_UNAVAILABLE', 'The clinic team will help find the right service.')
            state.update(reason=reason, doctor_id=doctor['id'], stage='date')
        if state['stage'] == 'date':
            day = resolve_day(text)
            if not day:
                return result('Which date? Say tomorrow, a weekday, or a date like 2026-10-01. All appointments use Dubai time.')
            slots = self.service.availability(state['doctor_id'], day)['slots']
            if not slots:
                return result('There are no demo slots that day. Please choose another date.')
            state.update(stage='slot', slots=slots, date=day)
            offered = ', '.join(f"{i+1}: {slot['label']}" for i, slot in enumerate(slots[:5]))
            return result(f"Available with {self.service.doctor(state['doctor_id'])['name']} on {day}: {offered}, Dubai time. Say first, second, or a time.", slots=slots)
        if state['stage'] == 'slot':
            slots = state['slots']
            index = self.selection(text, len(slots))
            slot = slots[index] if index is not None else None
            if slot is None:
                match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b', text)
                if match:
                    hour, minute = int(match[1]), int(match[2] or 0)
                    period = (match[3] or '').replace('.', '')
                    if period == 'pm' and hour < 12:
                        hour += 12
                    if period == 'am' and hour == 12:
                        hour = 0
                    slot = next((s for s in slots if datetime.fromisoformat(s['start']).astimezone(DUBAI).hour == hour and datetime.fromisoformat(s['start']).astimezone(DUBAI).minute == minute), None)
            if not slot:
                return result('Please choose an available time. You can say first or second.')
            invitation_email = {'client_email': client_email} if settings.calendar_invitations_enabled else {}
            response = self.service.prepare(uid, sid, state['doctor_id'], slot['start'], state['reason'],
                                            state.get('appointment_id'), **invitation_email)
            state.update(stage='confirmation', hold_id=response['hold_id'])
            return response
        return result('Please try saying book an appointment.')

    @staticmethod
    def selection(text, count):
        words = ['first', 'second', 'third', 'fourth', 'fifth']
        for index, word in enumerate(words):
            if word in text and index < count:
                return index
        if re.fullmatch(r'(number )?\d+', text):
            index = int(text.split()[-1]) - 1
            return index if 0 <= index < count else None
        return None

    def modify_target(self, state, target):
        if state['action'] == 'cancel':
            state['stage'] = 'cancel_confirmation'
            return result('Cancel ' + self.summary(target) + '? Say confirm or no.', requires_confirmation=True)
        state.update(stage='date', reason=target['reason'], doctor_id=target['doctor_id'])
        return result('Which new date would you prefer? Say tomorrow or a date.')

    def summary(self, appointment):
        start = datetime.fromisoformat(appointment['start']).astimezone(DUBAI)
        return f"{appointment['reason']} with {self.service.doctor(appointment['doctor_id'])['name']} on {start.strftime('%d %B at %I:%M %p')}, Dubai time"
