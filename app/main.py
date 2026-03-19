from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import mimetypes
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .config import settings
from .db import (
    create_glossary_entry,
    create_recording,
    get_recording,
    init_db,
    list_approved_memories,
    list_glossary,
    list_recent_recordings,
    update_recording,
)
from .services import build_translation_draft, find_glossary_hits, find_memory_hits, transcribe_audio


ALLOWED_AUDIO_TYPES = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.mount("/mobile-preview", StaticFiles(directory=Path(__file__).parent.parent / "mobile", html=True), name="mobile_preview")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class GlossaryCreateRequest(BaseModel):
    navajo_term: str = Field(min_length=1, max_length=120)
    english_meaning: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=400)


class DraftRefreshRequest(BaseModel):
    corrected_transcript: str = Field(default="", max_length=5000)
    translation_context: str = Field(default="", max_length=2000)


class ApprovalRequest(BaseModel):
    corrected_transcript: str = Field(min_length=1, max_length=5000)
    final_translation: str = Field(min_length=1, max_length=5000)
    translation_context: str = Field(default="", max_length=2000)
    translation_notes: str = Field(default="", max_length=800)
    topic_tags: str = Field(default="", max_length=300)


class SampleRecordingRequest(BaseModel):
    translation_context: str = Field(default="", max_length=2000)


def serialize_recording(recording: dict | None) -> dict | None:
    if recording is None:
        return None
    item = dict(recording)
    item["audio_url"] = f"/api/recordings/{recording['id']}/audio"
    return item


def validate_upload(file: UploadFile, size_bytes: int) -> str:
    return validate_audio_payload(file.filename or "", file.content_type or "", size_bytes)


def validate_audio_payload(filename: str, mime_type: str, size_bytes: int) -> str:
    extension = Path(filename).suffix.lower()
    guessed_extension = ALLOWED_AUDIO_TYPES.get(mime_type, extension)
    if guessed_extension not in {".mp3", ".m4a", ".wav", ".webm", ".ogg"}:
        raise HTTPException(status_code=400, detail="Please upload MP3, M4A, WAV, WEBM, or OGG audio.")
    if size_bytes > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail=f"Audio is too large. Keep uploads under {settings.max_upload_mb} MB for this MVP.",
        )
    return guessed_extension


def create_uploaded_recording(
    filename: str,
    mime_type: str,
    file_bytes: bytes,
    translation_context: str = "",
) -> dict:
    extension = validate_audio_payload(filename, mime_type, len(file_bytes))
    recording_id = str(uuid4())
    stored_path = settings.uploads_dir / f"{recording_id}{extension}"
    with stored_path.open("wb") as output_file:
        output_file.write(file_bytes)

    return create_recording(
        {
            "id": recording_id,
            "original_filename": filename or stored_path.name,
            "audio_path": str(stored_path),
            "mime_type": mime_type or "audio/mpeg",
            "status": "needs_review",
            "translation_context": translation_context.strip(),
            "processing_stage": "uploaded",
            "processing_message": "Audio uploaded. Ready to transcribe.",
        }
    )


def build_recording_response(recording_id: str) -> dict:
    recording = get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found.")
    return {"recording": serialize_recording(recording)}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "openai_configured": settings.openai_configured,
            "max_upload_mb": settings.max_upload_mb,
        },
    )


@app.get("/api/bootstrap")
async def bootstrap():
    sample_audio = settings.sample_audio_file
    return {
        "app": {
            "name": settings.app_name,
            "openai_configured": settings.openai_configured,
            "transcription_model": settings.transcription_model,
            "translation_model": settings.translation_model,
            "max_upload_mb": settings.max_upload_mb,
            "sample_audio_available": settings.sample_audio_available,
            "sample_audio_name": sample_audio.name if settings.sample_audio_available and sample_audio else "",
        },
        "glossary": list_glossary(),
        "recent_recordings": [serialize_recording(item) for item in list_recent_recordings()],
        "approved_examples": [serialize_recording(item) for item in list_approved_memories()],
    }


@app.get("/api/recordings/{recording_id}")
async def get_recording_endpoint(recording_id: str):
    return build_recording_response(recording_id)


@app.get("/api/recordings/{recording_id}/audio")
async def recording_audio(recording_id: str):
    recording = get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found.")
    return FileResponse(recording["audio_path"], media_type=recording["mime_type"] or "audio/mpeg")


@app.post("/api/recordings")
async def create_recording_endpoint(
    file: UploadFile = File(...),
    translation_context: str = Form(default=""),
):
    file_bytes = await file.read()
    validate_upload(file, len(file_bytes))
    recording = create_uploaded_recording(
        file.filename or "",
        file.content_type or "audio/mpeg",
        file_bytes,
        translation_context=translation_context,
    )
    return {"recording": serialize_recording(recording)}


@app.post("/api/recordings/from-sample")
async def create_recording_from_sample(request: SampleRecordingRequest | None = None):
    sample_path = settings.sample_audio_file
    if not sample_path or not sample_path.exists() or not sample_path.is_file():
        raise HTTPException(status_code=404, detail="Sample audio file is not available on this computer.")

    mime_type = mimetypes.guess_type(sample_path.name)[0] or "audio/mpeg"
    file_bytes = sample_path.read_bytes()
    recording = create_uploaded_recording(
        sample_path.name,
        mime_type,
        file_bytes,
        translation_context=request.translation_context if request else "",
    )
    return {"recording": serialize_recording(recording)}


@app.post("/api/recordings/{recording_id}/transcribe")
async def transcribe_recording(recording_id: str):
    recording = get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found.")

    update_recording(
        recording_id,
        {
            "processing_stage": "transcribing",
            "processing_message": f"Transcribing audio in chunks with {settings.transcription_model}...",
        },
    )

    glossary_entries = list_glossary()
    transcript, transcript_words, transcript_sections, warnings = transcribe_audio(
        Path(recording["audio_path"]),
        glossary_entries,
    )
    has_transcript = bool(transcript.strip())
    processing_stage = "transcribed" if has_transcript else "error"
    processing_message = (
        "Phonetic transcript ready. Review and correct it."
        if has_transcript
        else (warnings[0] if warnings else "Transcription finished, but no transcript was produced.")
    )

    updated = update_recording(
        recording_id,
        {
            "raw_transcript": transcript,
            "corrected_transcript": transcript,
            "transcript_words": transcript_words,
            "transcript_sections": transcript_sections,
            "meeting_gist": [],
            "meeting_summary": {},
            "warnings": warnings,
            "processing_stage": processing_stage,
            "processing_message": processing_message,
        },
    )
    return {"recording": serialize_recording(updated)}


@app.post("/api/recordings/{recording_id}/draft-translation")
async def draft_translation(recording_id: str):
    recording = get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found.")

    transcript_source = (recording.get("corrected_transcript") or recording.get("raw_transcript") or "").strip()
    translation_context = (recording.get("translation_context") or "").strip()
    update_recording(
        recording_id,
        {
            "processing_stage": "translating",
            "processing_message": f"Drafting meeting notes section by section with {settings.translation_model}...",
        },
    )

    glossary_entries = list_glossary()
    approved_examples = list_approved_memories(limit=40)
    glossary_hits = find_glossary_hits(transcript_source, glossary_entries)
    example_hits = find_memory_hits(transcript_source, approved_examples)
    draft = build_translation_draft(
        transcript_source,
        glossary_hits,
        example_hits,
        translation_context,
        transcript_sections=recording.get("transcript_sections", []),
    )

    processing_message = (
        "Meeting notes ready for review."
        if transcript_source
        else "Processing finished, but the transcript still needs to be entered or corrected manually."
    )

    updated = update_recording(
        recording_id,
        {
            "corrected_transcript": transcript_source,
            "draft_translation": draft["draft_translation"],
            "confidence": draft["confidence"],
            "draft_explanation": draft["draft_explanation"],
            "translation_context": translation_context,
            "glossary_hits": glossary_hits,
            "example_hits": example_hits,
            "transcript_sections": draft.get("transcript_sections", recording.get("transcript_sections", [])),
            "meeting_gist": draft.get("meeting_gist", []),
            "meeting_summary": draft.get("meeting_summary", {}),
            "processing_stage": "done",
            "processing_message": processing_message,
        },
    )
    return {"recording": serialize_recording(updated)}


@app.post("/api/recordings/{recording_id}/refresh-draft")
async def refresh_draft(recording_id: str, request: DraftRefreshRequest):
    recording = get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found.")

    glossary_entries = list_glossary()
    approved_examples = list_approved_memories(limit=40)
    glossary_hits = find_glossary_hits(request.corrected_transcript, glossary_entries)
    example_hits = find_memory_hits(request.corrected_transcript, approved_examples)
    draft = build_translation_draft(
        request.corrected_transcript,
        glossary_hits,
        example_hits,
        request.translation_context,
        transcript_sections=recording.get("transcript_sections", []),
    )
    updated = update_recording(
        recording_id,
        {
            "corrected_transcript": request.corrected_transcript.strip(),
            "draft_translation": draft["draft_translation"],
            "confidence": draft["confidence"],
            "draft_explanation": draft["draft_explanation"],
            "translation_context": request.translation_context.strip(),
            "glossary_hits": glossary_hits,
            "example_hits": example_hits,
            "transcript_sections": draft.get("transcript_sections", recording.get("transcript_sections", [])),
            "meeting_gist": draft.get("meeting_gist", []),
            "meeting_summary": draft.get("meeting_summary", {}),
            "processing_stage": "done",
            "processing_message": "Meeting notes ready for review.",
        },
    )
    return {"recording": serialize_recording(updated)}


@app.post("/api/recordings/{recording_id}/approve")
async def approve_recording(recording_id: str, request: ApprovalRequest):
    recording = get_recording(recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found.")

    updated = update_recording(
        recording_id,
        {
            "corrected_transcript": request.corrected_transcript.strip(),
            "final_translation": request.final_translation.strip(),
            "translation_context": request.translation_context.strip(),
            "translation_notes": request.translation_notes.strip(),
            "topic_tags": request.topic_tags.strip(),
            "status": "approved",
            "processing_stage": "approved",
            "processing_message": "Approved and saved to project memory.",
            "approved_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return {"recording": serialize_recording(updated)}


@app.post("/api/glossary")
async def create_glossary_endpoint(request: GlossaryCreateRequest):
    entry = create_glossary_entry(
        navajo_term=request.navajo_term,
        english_meaning=request.english_meaning,
        notes=request.notes,
    )
    return {"entry": entry}
