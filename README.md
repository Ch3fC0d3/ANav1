# ANav1

ANav1 is a lightweight human-in-the-loop Navajo translation workspace. It lets you upload or record audio, generate a rough phonetic transcript, draft English meaning ideas with project memory, and approve the final version so future suggestions improve over time.

## What it does

- Upload audio from phone or desktop
- Load a local sample MP3 with one click when `SAMPLE_AUDIO_PATH` is configured
- Record audio in the browser when supported
- Transcribe speech into rough Navajo phonetic text with `gpt-4o-transcribe`
- Break longer audio into smaller transcription chunks automatically before sending it to OpenAI
- Draft an English meaning using:
  - glossary matches
  - similar approved phrases
  - an OpenAI text model
- Let a human reviewer correct the Navajo transcript and English translation
- Save approved pairs as reusable project memory

## Stack

- FastAPI
- SQLite
- Vanilla HTML/CSS/JS
- OpenAI API for transcription and translation assist

## Quick start

1. Create a virtual environment and install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and add your OpenAI API key:

   ```powershell
   Copy-Item .env.example .env
   ```

3. Start the app:

   ```powershell
   uvicorn app.main:app --reload
   ```

4. Open [http://127.0.0.1:8000](http://127.0.0.1:8000)

## Environment variables

- `OPENAI_API_KEY`: required for automatic transcription and AI draft translation
- `OPENAI_TRANSCRIPTION_MODEL`: defaults to `gpt-4o-transcribe`
- `OPENAI_TRANSLATION_MODEL`: defaults to `gpt-4o-mini`
- `OPENAI_TRANSCRIPTION_LANGUAGE`: optional; leave blank to let the model detect language
- `OPENAI_TRANSCRIPTION_CHUNK_SECONDS`: defaults to `75`
- `MAX_UPLOAD_MB`: defaults to `25`
- `SAMPLE_AUDIO_PATH`: optional absolute path for a one-click local sample file button

## Railway deploy

The repo includes `railway.json`, so Railway has an explicit FastAPI start command and healthcheck path.

After connecting the GitHub repo in Railway, make sure you also:

1. Add `OPENAI_API_KEY` in the Railway service `Variables` tab.
2. Add a volume mounted at `/app/data` so SQLite and uploaded audio persist across deploys.
3. Generate a public domain in `Networking` if you want the app reachable from the web.

Optional Railway variables:

- `OPENAI_TRANSCRIPTION_MODEL`
- `OPENAI_TRANSLATION_MODEL`
- `OPENAI_TRANSCRIPTION_CHUNK_SECONDS`
- `MAX_UPLOAD_MB`

Leave `SAMPLE_AUDIO_PATH` blank on Railway unless that file exists inside the deployed container.

## Workflow

1. Capture or upload audio.
2. Review the rough phonetic transcript.
3. Edit the Navajo text if needed.
4. Refresh the AI draft translation after edits.
5. Approve the final English meaning.
6. Reuse approved phrases and glossary entries on future clips.

## Notes

- Without `OPENAI_API_KEY`, the app still opens and stores sessions, but transcript and AI draft generation are manual.
- Long audio chunking uses `imageio-ffmpeg` so the server can split recordings before transcription.
- Approved phrases are stored locally in `data/app.db`.
- Uploaded audio files are stored in `data/uploads/`.
