"""Run separately: python -m app.voice_agent dev"""
import json
import logging
from pathlib import Path

from dotenv import load_dotenv

# LiveKit reads its connection environment while its modules are imported.
# Load this backend's explicit credentials before importing the SDK.
load_dotenv(Path(__file__).resolve().parent.parent / '.env', override=True)

import httpx
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, function_tool, inference
from livekit.plugins import google, silero

from .config import settings

INSTRUCTIONS = '''You are Noor, the friendly voice receptionist for a Dubai dental clinic.
Speak naturally, concisely, and in the patient's language (English, Hindi/Hinglish, or Arabic).
All dates and times use Asia/Dubai. Use get_context for today's date. Clarify ambiguous dates.
Help with booking, viewing, rescheduling and cancellation only; do not diagnose conditions.
The backend owns availability. ALWAYS call check_availability; offer only its returned slots.
Ask service and date as needed. Get doctors from tools and choose the single available doctor automatically.
Do not speak the connected account name, email, calendar owner or technical setup details.
Refer to the clinic team until the administrator configures the doctor list. Never invent doctor or appointment IDs.
For rescheduling, get the patient's appointments first and pass the chosen appointment_id to prepare_booking.
If there are multiple appointments, ask which to modify.
Call prepare_booking to hold the exact slot. Read back date, Dubai time and service.
Ask for explicit confirmation. Do not call confirm_action in the same turn as
prepare_booking or prepare_cancellation. Wait for the patient to affirm the summary in a later turn.
For cancellation call prepare_cancellation, read the summary and ask confirmation, then wait.
Only call confirm_action after the patient explicitly says yes/confirm to the pending action.
Only say a Google Calendar event was created when confirm_action returns calendar_event_created=true.
When get_context returns booking_mode=local_demo, clearly say after booking: "This is a local test booking; no actual calendar event or email was sent."
When booking_mode=google_calendar, only say the doctor's calendar was updated and the customer invitation was sent when confirm_action returns customer_invited=true.
Only say a separate confirmation email was sent when confirm_action returns confirmation_email_sent=true.
On no, call release_hold. Never say confirmed unless the tool returns ok=true.
On booking errors, relay the human-assistance message in the patient's language.
If handoff_requested=true, say the clinic team has been asked to contact them and guide the booking.
Never claim a live transfer or callback time. If handoff_requested=false, say the request could not be saved and ask them to contact reception.
Do not retry a failed confirmation automatically or claim a failed booking succeeded. Holds last 120 seconds.
Known patient name is supplied; do not ask for email/name again.''' 

FIRESTORE_INSTRUCTIONS = '''You are Noor, a friendly dental appointment receptionist.
Speak concisely in the caller's language, including Hindi/Hinglish. No sign-in is needed.
Your job is to collect the client name, phone number, appointment date and time, and save a booking.
The email for calendar invitations is handled automatically from the sign-in session. NEVER mention, read back, or ask about the email address.
Use get_context for today's date and clinic timezone. Clarify ambiguous dates and AM/PM.
Use get_doctors and check_availability; only offer returned slots. Choose the single doctor automatically.
Ask the caller for their name and phone number. Preserve country codes and leading zeroes.
Do not invent a phone number or use a generic guest name. Read the phone digits back clearly.
The service/reason is optional; use Appointment if the caller does not specify it.
Call prepare_booking with the actual client_name and phone_number and an available slot.
Read back ONLY the name, phone number, date and clinic-local time. Do NOT mention the email. Ask whether the details are correct.
Never call confirm_action in the same turn as prepare_booking or prepare_cancellation.
Wait for the caller to confirm in a later turn; on no, release_hold or correct the details.
Only say the appointment is booked after confirm_action returns ok=true.
Bookings are saved in Firebase. Only say Google sent a Calendar invite when confirm_action returns customer_invited=true.
For retryable errors, explain the returned message and ask for corrected details or a new slot.
For other failures say booking could not be verified and ask the caller to check their appointments.
Only say staff have been asked to help if handoff_requested=true. Never promise a callback time.
For viewing or cancelling use get_appointments. Only operate on IDs returned by tools.
For rescheduling pass the selected appointment_id and collect the name and phone again if unavailable.
Never retry a failed confirmation automatically. Holds expire after 120 seconds.'''


class DentalAssistant(Agent):
    def __init__(self, metadata: dict, client: httpx.AsyncClient):
        instructions = (FIRESTORE_INSTRUCTIONS + '\nVerified patient name: ' + metadata.get('patient_name', 'Patient') +
                        '\nVerified patient email: ' + metadata.get('patient_email', '')) if settings.booking_mode in {'firestore', 'firestore_calendar'} else INSTRUCTIONS + '\nPatient name: ' + metadata['patient_name']
        super().__init__(instructions=instructions)
        self.metadata, self.client = metadata, client

    async def call(self, name, **arguments):
        logger.info('[TOOL CALL] %s  args=%s  session=%s', name, arguments, self.metadata.get('session_id'))
        try:
            response = await self.client.post(f"/internal/voice/{self.metadata['session_id']}/tools",
                headers={'Authorization': 'Bearer ' + self.metadata['service_token']},
                json={'name': name, 'arguments': arguments})
            response.raise_for_status()
            data = response.json()
            logger.info('[TOOL RESPONSE] %s  ok=%s  message=%s', name, data.get('ok'), data.get('message', '')[:200])
            return data
        except (httpx.HTTPError, ValueError) as exc:
            logger.error('[TOOL ERROR] %s  exception=%s  response_status=%s  response_body=%s',
                         name, exc,
                         getattr(exc, 'response', None) and exc.response.status_code,
                         getattr(exc, 'response', None) and exc.response.text[:500] if hasattr(exc, 'response') and exc.response is not None else 'N/A')
            return {'ok': False, 'code': 'BACKEND_UNAVAILABLE', 'handoff_requested': False,
                    'message': 'I could not verify the booking or reach the clinic team. Please contact reception for help; check your appointments before booking again.'}

    @function_tool()
    async def get_context(self):
        """Get current clinic date/time to resolve relative dates in Dubai."""
        from datetime import datetime
        from .appointments import DUBAI
        return {'clinic_time': datetime.now(DUBAI).isoformat(), 'timezone': settings.clinic_timezone, 'booking_mode': settings.booking_mode}

    @function_tool()
    async def get_doctors(self):
        """List actual demo doctors and supported services."""
        return await self.call('get_doctors')

    @function_tool()
    async def get_appointments(self):
        """Retrieve only this authenticated patient's upcoming appointments."""
        return await self.call('get_appointments')

    @function_tool()
    async def check_availability(self, doctor_id: str, date: str):
        """Return available demo slots. Date must be YYYY-MM-DD in Dubai timezone."""
        return await self.call('check_availability', doctor_id=doctor_id, date=date)

    @function_tool()
    async def prepare_booking(self, doctor_id: str, start: str, reason: str = 'Appointment', appointment_id: str | None = None,
                              client_name: str = '', phone_number: str = '', client_email: str = ''):
        """Hold a returned UTC slot; return exact summary to ask confirmation. Optional appointment_id reschedules."""
        logger.info('[PREPARE_BOOKING] client_name=%r  phone_number=%r  doctor_id=%r  start=%r  reason=%r  email=%r',
                    client_name, phone_number, doctor_id, start, reason, self.metadata.get('patient_email') or client_email)
        return await self.call('prepare_booking', doctor_id=doctor_id, start=start, reason=reason, appointment_id=appointment_id,
                               client_name=client_name, phone_number=phone_number,
                               client_email=self.metadata.get('patient_email') or client_email)

    @function_tool()
    async def prepare_cancellation(self, appointment_id: str):
        """Prepare cancellation and return the appointment summary; does not cancel yet."""
        return await self.call('prepare_cancellation', appointment_id=appointment_id)

    @function_tool()
    async def confirm_action(self):
        """Commit a pending action ONLY after patient explicitly affirms the exact summary in a later turn."""
        return await self.call('confirm_action')

    @function_tool()
    async def release_hold(self):
        """Release a held slot and abandon the pending action when the patient declines."""
        return await self.call('release_hold')


server = AgentServer()
logger = logging.getLogger('noor.voice_agent')


@server.rtc_session(agent_name=settings.livekit_agent_name)
async def dental_voice(ctx: agents.JobContext):
    settings.validate_runtime()
    if not settings.google_api_key:
        raise ValueError('Add GOOGLE_API_KEY to backend/.env for Gemini conversation and booking tools.')
    try:
        metadata = json.loads(ctx.job.metadata or '{}')
    except (TypeError, json.JSONDecodeError):
        logger.error('Ignoring LiveKit job with invalid session metadata; start calls from the Noor app.')
        return
    required_metadata = ('session_id', 'patient_name', 'service_token')
    if not isinstance(metadata, dict) or any(not metadata.get(key) for key in required_metadata):
        logger.error('Ignoring LiveKit job without patient session metadata; start calls from the Noor app, not a bare console room.')
        return
    if settings.voice_pipeline == 'gemini_live':
        if not settings.google_api_key:
            raise ValueError('GOOGLE_API_KEY is required for gemini_live')
        session = AgentSession(llm=google.realtime.RealtimeModel(api_key=settings.google_api_key,
            model=settings.gemini_live_model, voice=settings.gemini_voice))
    else:
        session = AgentSession(
            stt=inference.STT(model=settings.stt_model, language=settings.stt_language),
            llm=inference.LLM(model=settings.llm_model),
            tts=inference.TTS(model=settings.tts_model, voice=settings.tts_voice),
            vad=silero.VAD.load(), turn_detection='vad',
            # Gemini rejects a history containing an interrupted/preempted
            # assistant function call without its matching function response.
            # Booking uses several function tools, so wait until the caller's
            # turn is committed before starting generation.
            turn_handling={'preemptive_generation': {'enabled': False}})
    async with httpx.AsyncClient(base_url=settings.backend_url, timeout=120) as client:
        await session.start(room=ctx.room, agent=DentalAssistant(metadata, client))
        mode_note = ('Greet the caller briefly by name. Ask how you can help. Do not mention email, accounts, or technical details.'
                     if settings.booking_mode in {'firestore', 'firestore_calendar'} else 'Ask how you can help with their appointment. Do not mention host accounts or technical setup.'
                     if settings.booking_mode == 'google_calendar'
                     else 'Explain briefly that this is a local demo and does not create real calendar events.')
        await session.generate_reply(instructions=mode_note)
        # AgentSession.start returns before the call ends. Keep the tool HTTP client alive.
        import asyncio
        finished = asyncio.Event()
        ctx.add_shutdown_callback(lambda: _finish(finished))
        await finished.wait()


async def _finish(event):
    event.set()


if __name__ == '__main__':
    agents.cli.run_app(server)
