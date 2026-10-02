"""Lightweight in-process store for text chat session state."""
import threading

_lock = threading.Lock()
_sessions: dict[str, dict] = {}


def get_chat_session_state(session_id: str) -> dict:
    with _lock:
        return dict(_sessions.get(session_id, {}))


def save_chat_session_state(session_id: str, state: dict) -> None:
    with _lock:
        _sessions[session_id] = dict(state)


def delete_chat_session(session_id: str) -> None:
    with _lock:
        _sessions.pop(session_id, None)
