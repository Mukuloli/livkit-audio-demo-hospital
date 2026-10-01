# livkit-audio-demo-hospital

Backend for the Noor hospital appointment voice demo, built with FastAPI and LiveKit Agents.

Install dependencies from `requirements.txt`, configure your local `.env`, and run:

```sh
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
python -m app.voice_agent dev
```

Environment files, credentials, local data, and logs are excluded from Git.
