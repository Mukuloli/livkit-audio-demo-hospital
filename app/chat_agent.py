"""Text chat agent powered by Google ADK v1.39 — same Firestore DB as voice agent."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai import types as genai_types

from .appointments import BookingError, result
from .config import settings

logger = logging.getLogger('noor.chat')

APP_NAME = 'noor-chat'

CHAT_INSTRUCTIONS = """You are Noor, a friendly dental appointment assistant.
Help the user book, view, reschedule or cancel appointments through text chat.
Speak naturally in the user's language (English, Hindi, or Hinglish).

Rules:
- Use get_context to know today's date and timezone.
- Use get_doctors to find available doctors.
- Use check_availability to find free slots before offering any time.
- To book: collect client_name and phone_number first. Email is optional (handled automatically).
- Call prepare_booking once you have name, phone, doctor, date and slot.
- After prepare_booking: confirm ONLY name, phone, date, time. Do NOT mention email.
- Only call confirm_booking after the user explicitly says yes/confirm/haan/okay.
- If user says no/nahi/cancel → call release_hold.
- To view appointments: call get_appointments.
- To cancel: call get_appointments first, then prepare_cancellation, then confirm_booking.
- Never invent slots, doctor IDs, or appointment IDs.
- Be concise. Don't overwhelm with long messages.
"""

# One shared session service (persists conversation history per session)
_session_service = InMemorySessionService()


def _build_tools(service, uid: str, session_id: str) -> list:
    """Create ADK FunctionTools bound to this user's session."""

    def get_context() -> dict:
        """Get current clinic date and time in Dubai timezone."""
        tz = ZoneInfo(settings.clinic_timezone)
        return {
            'clinic_time': datetime.now(tz).isoformat(),
            'timezone': settings.clinic_timezone,
            'booking_mode': settings.booking_mode,
        }

    def get_doctors() -> dict:
        """List available doctors and their services."""
        try:
            doctors = service.public_doctors() if hasattr(service, 'public_doctors') else []
            return result('Available doctors.', doctors=doctors)
        except Exception as exc:
            return {'ok': False, 'message': str(exc)}

    def get_appointments() -> dict:
        """Get the signed-in user's upcoming confirmed appointments."""
        try:
            return service.appointments(uid)
        except Exception as exc:
            return {'ok': False, 'message': str(exc)}

    def check_availability(doctor_id: str, date: str) -> dict:
        """Check available time slots for a doctor on a given date (YYYY-MM-DD in Dubai timezone)."""
        try:
            return service.availability(doctor_id, date)
        except BookingError as exc:
            return {'ok': False, 'code': exc.code, 'message': exc.message}
        except Exception as exc:
            return {'ok': False, 'message': str(exc)}

    def prepare_booking(
        doctor_id: str,
        start: str,
        client_name: str,
        phone_number: str,
        reason: str = 'Appointment',
        client_email: str = '',
        appointment_id: str = '',
    ) -> dict:
        """Hold a slot for confirmation. Returns a summary. Use appointment_id only when rescheduling."""
        try:
            kwargs = {}
            if settings.booking_mode in {'firestore', 'firestore_calendar'}:
                kwargs = {
                    'client_name': client_name,
                    'phone_number': phone_number,
                    'client_email': client_email,
                }
            response = service.prepare(
                uid, session_id, doctor_id, start,
                reason, appointment_id or None, **kwargs
            )
            # Save hold state for confirm step
            if response.get('ok') and response.get('hold_id'):
                from .app_state import save_chat_session_state
                save_chat_session_state(session_id, {
                    'stage': 'confirmation',
                    'hold_id': response['hold_id'],
                })
            return response
        except BookingError as exc:
            return {'ok': False, 'code': exc.code, 'message': exc.message, 'retryable': True}
        except Exception as exc:
            return {'ok': False, 'message': str(exc)}

    def confirm_booking() -> dict:
        """Confirm the pending booking or cancellation after user says yes."""
        from .app_state import get_chat_session_state, save_chat_session_state
        state = get_chat_session_state(session_id)
        try:
            if state.get('stage') == 'confirmation':
                response = service.confirm(uid, session_id, state['hold_id'])
                save_chat_session_state(session_id, {})
                return response
            elif state.get('stage') == 'cancel_confirmation':
                response = service.cancel(uid, state['appointment_id'])
                save_chat_session_state(session_id, {})
                return response
            else:
                return {'ok': False, 'message': 'Nothing to confirm. Please prepare a booking first.'}
        except BookingError as exc:
            return {'ok': False, 'code': exc.code, 'message': exc.message}
        except Exception as exc:
            return {'ok': False, 'message': str(exc)}

    def release_hold() -> dict:
        """Release the current slot hold when user says no or wants to change details."""
        from .app_state import save_chat_session_state
        try:
            response = service.release(uid, session_id)
            save_chat_session_state(session_id, {})
            return response
        except Exception as exc:
            return {'ok': False, 'message': str(exc)}

    def prepare_cancellation(appointment_id: str) -> dict:
        """Prepare cancellation of a specific appointment by ID. Returns summary for confirmation."""
        from .app_state import save_chat_session_state
        try:
            appointments = service.appointments(uid).get('appointments', [])
            target = next((a for a in appointments if a['id'] == appointment_id), None)
            if not target:
                return {'ok': False, 'message': 'Appointment not found in your upcoming bookings.'}
            save_chat_session_state(session_id, {
                'stage': 'cancel_confirmation',
                'appointment_id': appointment_id,
            })
            return result('Confirm cancellation of this appointment?', appointment=target, requires_confirmation=True)
        except Exception as exc:
            return {'ok': False, 'message': str(exc)}

    return [
        FunctionTool(get_context),
        FunctionTool(get_doctors),
        FunctionTool(get_appointments),
        FunctionTool(check_availability),
        FunctionTool(prepare_booking),
        FunctionTool(confirm_booking),
        FunctionTool(release_hold),
        FunctionTool(prepare_cancellation),
    ]


async def chat(uid: str, session_id: str, message: str, service) -> str:
    """Process one user message and return Noor's text reply."""
    tools = _build_tools(service, uid, session_id)

    # Model: use plain model name (ADK uses google-genai under the hood)
    model_name = settings.gemini_model
    if model_name.startswith('google/'):
        model_name = model_name[len('google/'):]

    agent = LlmAgent(
        name='noor',
        model=model_name,
        instruction=CHAT_INSTRUCTIONS,
        tools=tools,
    )

    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=_session_service,
    )

    # Reuse or create session so conversation history is preserved
    adk_session_id = f'chat-{uid}-{session_id}'
    try:
        await _session_service.get_session(
            app_name=APP_NAME, user_id=uid, session_id=adk_session_id
        )
    except Exception:
        await _session_service.create_session(
            app_name=APP_NAME, user_id=uid, session_id=adk_session_id
        )

    user_content = genai_types.Content(
        role='user',
        parts=[genai_types.Part(text=message)],
    )

    reply_parts: list[str] = []
    async for event in runner.run_async(
        user_id=uid,
        session_id=adk_session_id,
        new_message=user_content,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, 'text') and part.text:
                    reply_parts.append(part.text)

    reply = ' '.join(reply_parts).strip()
    logger.info('[CHAT REPLY] uid=%s len=%d', uid, len(reply))
    return reply or "I'm sorry, I couldn't process that. Please try again."
