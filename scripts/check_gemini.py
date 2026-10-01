"""Read-only Gemini tool-calling smoke check; never books an appointment."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google import genai
from google.genai import types
from app.config import settings

try:
    with genai.Client(api_key=settings.google_api_key, http_options={'timeout': 30000}) as client:
        response = client.models.generate_content(model=settings.gemini_model,
            contents='Use check_availability for 2026-10-05. This is a read-only test.',
            config=types.GenerateContentConfig(
                tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
                    name='check_availability', description='Read available slots',
                    parameters={'type': 'object', 'properties': {'date': {'type': 'string'}}, 'required': ['date']})])],
                tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode='ANY'))))
        calls = response.function_calls or []
        assert calls and calls[0].name == 'check_availability'
        print('Gemini API and function calling: passed. No booking executed.')
except Exception as exc:
    print('Gemini check failed:', type(exc).__name__, 'status:', getattr(exc, 'code', 'unavailable'))
    message = str(getattr(exc, 'message', ''))
    for key in (settings.google_api_key,):
        if key:
            message = message.replace(key, '[redacted]')
    print(message[:800])
    sys.exit(1)
