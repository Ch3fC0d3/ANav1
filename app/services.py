from __future__ import annotations

from contextlib import contextmanager
import json
import re
import shutil
import subprocess
import unicodedata
import wave
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from uuid import uuid4

from openai import OpenAI

from .config import settings

try:
    import imageio_ffmpeg
except Exception:
    imageio_ffmpeg = None


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def tokenize(value: str) -> list[str]:
    return re.findall(r"[a-zA-Z']+", normalize_text(value))


def ascii_phonetic_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^A-Za-z0-9' -]+", " ", ascii_text)
    return re.sub(r"\s+", " ", ascii_text).strip()


def find_glossary_hits(transcript: str, glossary_entries: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    normalized_transcript = normalize_text(transcript)
    if not normalized_transcript:
        return []

    hits: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    for entry in glossary_entries:
        term = normalize_text(entry["navajo_term"])
        if not term or entry["id"] in seen_ids:
            continue
        if term in normalized_transcript:
            seen_ids.add(entry["id"])
            hits.append(
                {
                    "id": entry["id"],
                    "navajo_term": entry["navajo_term"],
                    "english_meaning": entry["english_meaning"],
                    "notes": entry["notes"],
                    "match_type": "phrase",
                }
            )

    if len(hits) >= limit:
        return hits[:limit]

    transcript_tokens = set(tokenize(transcript))
    for entry in glossary_entries:
        if entry["id"] in seen_ids:
            continue
        term_tokens = set(tokenize(entry["navajo_term"]))
        if term_tokens and term_tokens.issubset(transcript_tokens):
            seen_ids.add(entry["id"])
            hits.append(
                {
                    "id": entry["id"],
                    "navajo_term": entry["navajo_term"],
                    "english_meaning": entry["english_meaning"],
                    "notes": entry["notes"],
                    "match_type": "token",
                }
            )
        if len(hits) >= limit:
            break
    return hits[:limit]


def _similarity_score(source: str, target: str) -> float:
    source_norm = normalize_text(source)
    target_norm = normalize_text(target)
    if not source_norm or not target_norm:
        return 0.0

    sequence_score = SequenceMatcher(None, source_norm, target_norm).ratio()
    source_tokens = set(tokenize(source_norm))
    target_tokens = set(tokenize(target_norm))
    overlap = len(source_tokens & target_tokens) / max(len(source_tokens | target_tokens), 1)
    return (sequence_score * 0.65) + (overlap * 0.35)


def find_memory_hits(transcript: str, approved_examples: list[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for example in approved_examples:
        source_text = example.get("corrected_transcript") or example.get("raw_transcript") or ""
        score = _similarity_score(transcript, source_text)
        if score < 0.12:
            continue
        ranked.append(
            (
                score,
                {
                    "id": example["id"],
                    "score": round(score, 3),
                    "corrected_transcript": source_text,
                    "final_translation": example.get("final_translation", ""),
                    "topic_tags": example.get("topic_tags", ""),
                },
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in ranked[:limit]]


def _build_transcription_prompt(glossary_entries: list[dict[str, Any]]) -> str:
    terms: list[str] = [
        (
            "Navajo-language audio. Transcribe the speech phonetically using plain English letters. "
            "Do not translate into English. If speech is unclear, keep approximate sounds instead of inventing English phrases."
        )
    ]
    seen: set[str] = set()
    for entry in glossary_entries[:30]:
        term = entry["navajo_term"].strip()
        if not term:
            continue
        normalized = normalize_text(term)
        if normalized in seen:
            continue
        seen.add(normalized)
        terms.append(term)
    return ", ".join(terms)


def rewrite_as_english_letter_phonetics(transcript: str, glossary_entries: list[dict[str, Any]]) -> str:
    del glossary_entries
    return ascii_phonetic_text(transcript.strip())


def _build_transcription_request_kwargs(glossary_entries: list[dict[str, Any]]) -> dict[str, Any]:
    model_name = settings.transcription_model
    request_kwargs: dict[str, Any] = {
        "model": model_name,
    }
    if model_name == "whisper-1":
        request_kwargs["response_format"] = "verbose_json"
        request_kwargs["timestamp_granularities"] = ["word"]
    else:
        request_kwargs["response_format"] = "json"

    prompt = _build_transcription_prompt(glossary_entries)
    if prompt:
        request_kwargs["prompt"] = prompt
    if settings.normalized_transcription_language:
        request_kwargs["language"] = settings.normalized_transcription_language
    return request_kwargs


def _resolve_ffmpeg_executable() -> str | None:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return ffmpeg_path
    if imageio_ffmpeg is None:
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _extract_transcript_words(transcript: Any, time_offset_seconds: float = 0.0) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for item in getattr(transcript, "words", []) or []:
        start = getattr(item, "start", None)
        end = getattr(item, "end", None)
        words.append(
            {
                "word": getattr(item, "word", ""),
                "start": start + time_offset_seconds if start is not None else None,
                "end": end + time_offset_seconds if end is not None else None,
            }
        )
    return words


def _read_wav_duration_seconds(audio_path: Path) -> float | None:
    if audio_path.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(audio_path), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            if frame_rate <= 0:
                return None
            return wav_file.getnframes() / frame_rate
    except (OSError, wave.Error):
        return None


def _format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return "Unknown"

    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _build_time_range_label(start_seconds: float | None, end_seconds: float | None) -> str:
    if start_seconds is None and end_seconds is None:
        return "Section"
    if end_seconds is None:
        return _format_timestamp(start_seconds)
    return f"{_format_timestamp(start_seconds)}-{_format_timestamp(end_seconds)}"


def _build_transcript_section(
    transcript: str,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    start_value = round(start_seconds, 1) if start_seconds is not None else None
    end_value = round(end_seconds, 1) if end_seconds is not None else None
    if start_value is not None and end_value is not None:
        end_value = max(start_value, end_value)

    return {
        "label": label or _build_time_range_label(start_value, end_value),
        "start_seconds": start_value,
        "end_seconds": end_value,
        "transcript": transcript.strip(),
    }


@contextmanager
def _prepare_transcription_chunks(audio_path: Path, chunk_seconds: int | None = None):
    chunk_seconds = max(30, chunk_seconds or settings.transcription_chunk_seconds)
    ffmpeg_path = _resolve_ffmpeg_executable()

    if not ffmpeg_path:
        yield [(audio_path, 0.0, None)], []
        return

    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = settings.uploads_dir / f"anav1-transcribe-{uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        output_pattern = temp_dir / "chunk-%03d.wav"
        command = [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(audio_path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "segment",
            "-segment_time",
            str(chunk_seconds),
            "-segment_format",
            "wav",
            "-reset_timestamps",
            "1",
            str(output_pattern),
        ]

        try:
            subprocess.run(command, capture_output=True, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = ""
            if isinstance(exc, subprocess.CalledProcessError):
                detail = (
                    exc.stderr.decode("utf-8", "ignore").strip()
                    or exc.stdout.decode("utf-8", "ignore").strip()
                )
            elif str(exc):
                detail = str(exc)

            warning = "Automatic chunking was unavailable, so the file was sent as one transcription request."
            if detail:
                warning = f"{warning} Chunking detail: {detail}"
            yield [(audio_path, 0.0, None)], [warning]
            return

        chunk_paths = sorted(temp_dir.glob("chunk-*.wav"))
        if not chunk_paths:
            yield [(audio_path, 0.0, None)], ["Chunking produced no audio segments, so the file was sent as one transcription request."]
            return

        warnings: list[str] = []
        if len(chunk_paths) > 1:
            warnings.append(
                f"Long audio was split into {len(chunk_paths)} chunks of about {chunk_seconds} seconds each before transcription."
            )
        yield [
            (chunk_path, index * chunk_seconds, _read_wav_duration_seconds(chunk_path))
            for index, chunk_path in enumerate(chunk_paths)
        ], warnings
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _transcribe_chunk_with_fallback(
    client: OpenAI,
    chunk_path: Path,
    glossary_entries: list[dict[str, Any]],
    request_kwargs: dict[str, Any],
    time_offset_seconds: float,
    duration_hint_seconds: float | None,
    chunk_seconds: int,
    label: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    try:
        with chunk_path.open("rb") as audio_file:
            transcript = client.audio.transcriptions.create(file=audio_file, **request_kwargs)
    except Exception as exc:
        return _retry_chunk_with_smaller_segments(
            client,
            chunk_path,
            glossary_entries,
            request_kwargs,
            time_offset_seconds,
            duration_hint_seconds,
            chunk_seconds,
            f"{label} could not be transcribed: {exc}",
            label,
        )

    raw_text = (getattr(transcript, "text", "") or "").strip()
    phonetic_text = rewrite_as_english_letter_phonetics(raw_text, glossary_entries)
    if phonetic_text:
        words = _extract_transcript_words(transcript, time_offset_seconds)
        if words and words[-1].get("end") is not None:
            end_seconds = words[-1]["end"]
        elif duration_hint_seconds is not None:
            end_seconds = time_offset_seconds + duration_hint_seconds
        else:
            end_seconds = time_offset_seconds + chunk_seconds
        return [
            _build_transcript_section(
                phonetic_text,
                start_seconds=time_offset_seconds,
                end_seconds=end_seconds,
                label=_build_time_range_label(time_offset_seconds, end_seconds),
            )
        ], words, []

    return _retry_chunk_with_smaller_segments(
        client,
        chunk_path,
        glossary_entries,
        request_kwargs,
        time_offset_seconds,
        duration_hint_seconds,
        chunk_seconds,
        f"{label} returned no transcript text.",
        label,
    )


def _retry_chunk_with_smaller_segments(
    client: OpenAI,
    chunk_path: Path,
    glossary_entries: list[dict[str, Any]],
    request_kwargs: dict[str, Any],
    time_offset_seconds: float,
    duration_hint_seconds: float | None,
    chunk_seconds: int,
    primary_warning: str,
    label: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    next_chunk_seconds = max(30, chunk_seconds // 2)
    if next_chunk_seconds >= chunk_seconds:
        return [], [], [primary_warning]

    with _prepare_transcription_chunks(chunk_path, next_chunk_seconds) as (retry_chunks, retry_warnings):
        retry_chunk_list = list(retry_chunks)
        if len(retry_chunk_list) <= 1:
            warnings = [primary_warning]
            warnings.extend(retry_warnings)
            return [], [], warnings

        warnings = [f"{primary_warning} Retrying that section with smaller chunks."]
        warnings.extend(retry_warnings)
        transcript_parts: list[dict[str, Any]] = []
        words: list[dict[str, Any]] = []

        for retry_index, (retry_chunk_path, retry_offset_seconds, retry_duration_seconds) in enumerate(
            retry_chunk_list,
            start=1,
        ):
            nested_label = f"{label}.{retry_index}"
            retry_texts, retry_words, nested_warnings = _transcribe_chunk_with_fallback(
                client,
                retry_chunk_path,
                glossary_entries,
                request_kwargs,
                time_offset_seconds + retry_offset_seconds,
                retry_duration_seconds,
                next_chunk_seconds,
                nested_label,
            )
            transcript_parts.extend(retry_texts)
            words.extend(retry_words)
            warnings.extend(nested_warnings)

        if transcript_parts:
            return transcript_parts, words, warnings

        return [], words, warnings


def transcribe_audio(
    audio_path: Path,
    glossary_entries: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    if not settings.openai_configured:
        return "", [], [], ["OpenAI API key not configured. Add the transcript manually, then refresh the draft translation."]

    client = OpenAI(api_key=settings.openai_api_key)
    request_kwargs = _build_transcription_request_kwargs(glossary_entries)
    warnings: list[str] = []
    if settings.transcription_language_warning:
        warnings.append(settings.transcription_language_warning)
    transcript_sections: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []

    top_level_chunk_seconds = max(30, settings.transcription_chunk_seconds)
    with _prepare_transcription_chunks(audio_path, top_level_chunk_seconds) as (audio_chunks, chunk_warnings):
        warnings.extend(chunk_warnings)

        for chunk_index, (chunk_path, time_offset_seconds, duration_hint_seconds) in enumerate(audio_chunks, start=1):
            chunk_sections, chunk_words, chunk_warnings = _transcribe_chunk_with_fallback(
                client,
                chunk_path,
                glossary_entries,
                request_kwargs,
                time_offset_seconds,
                duration_hint_seconds,
                top_level_chunk_seconds,
                f"Chunk {chunk_index}",
            )
            transcript_sections.extend(chunk_sections)
            words.extend(chunk_words)
            warnings.extend(chunk_warnings)

    if not transcript_sections:
        return "", words, [], warnings or ["Automatic transcription failed for every audio chunk."]

    transcript_text = "\n\n".join(section["transcript"] for section in transcript_sections if section.get("transcript")).strip()
    return transcript_text, words, transcript_sections, warnings


def _split_text_block(value: str, target_chars: int) -> list[str]:
    words = value.split()
    if not words:
        return []

    parts: list[str] = []
    current_words: list[str] = []
    current_length = 0

    for word in words:
        extra = len(word) if not current_words else len(word) + 1
        if current_words and current_length + extra > target_chars:
            parts.append(" ".join(current_words))
            current_words = [word]
            current_length = len(word)
            continue
        current_words.append(word)
        current_length += extra

    if current_words:
        parts.append(" ".join(current_words))
    return parts


def _split_transcript_for_translation(transcript: str, target_chars: int = 700) -> list[str]:
    blocks = [block.strip() for block in re.split(r"\n{2,}", transcript) if block.strip()]
    if not blocks:
        stripped = transcript.strip()
        return [stripped] if stripped else []

    sections: list[str] = []
    current_blocks: list[str] = []
    current_length = 0

    for block in blocks:
        block_parts = _split_text_block(block, target_chars) if len(block) > target_chars else [block]
        for part in block_parts:
            extra = len(part) if not current_blocks else len(part) + 2
            if current_blocks and current_length + extra > target_chars:
                sections.append("\n\n".join(current_blocks))
                current_blocks = [part]
                current_length = len(part)
                continue

            current_blocks.append(part)
            current_length += extra

    if current_blocks:
        sections.append("\n\n".join(current_blocks))
    return sections


def _filter_glossary_hits_for_section(section: str, glossary_hits: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    normalized_section = normalize_text(section)
    section_tokens = set(tokenize(section))
    matches: list[dict[str, Any]] = []

    for hit in glossary_hits:
        term = normalize_text(hit.get("navajo_term", ""))
        if not term:
            continue
        term_tokens = set(tokenize(hit.get("navajo_term", "")))
        if term in normalized_section or (term_tokens and term_tokens.issubset(section_tokens)):
            matches.append(hit)
        if len(matches) >= limit:
            break

    return matches


def _filter_memory_hits_for_section(section: str, memory_hits: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for hit in memory_hits:
        source_text = hit.get("corrected_transcript") or ""
        score = _similarity_score(section, source_text)
        if score < 0.08:
            continue
        ranked.append((score, hit))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in ranked[:limit]]


def _combine_confidence_levels(levels: list[str]) -> str:
    if not levels:
        return "low"
    if "low" in levels:
        return "low"
    if "medium" in levels:
        return "medium"
    return "high"


def _split_text_into_count(value: str, count: int) -> list[str]:
    words = value.split()
    if not words:
        return []
    if count <= 1:
        return [" ".join(words)]

    base_size, remainder = divmod(len(words), count)
    parts: list[str] = []
    cursor = 0
    for index in range(count):
        take_count = base_size + (1 if index < remainder else 0)
        if take_count <= 0 and cursor < len(words):
            take_count = 1
        if take_count <= 0:
            break
        chunk = " ".join(words[cursor: cursor + take_count]).strip()
        if chunk:
            parts.append(chunk)
        cursor += take_count

    if cursor < len(words) and parts:
        parts[-1] = f"{parts[-1]} {' '.join(words[cursor:])}".strip()

    return [part for part in parts if part]


def _fallback_transcript_sections(transcript: str) -> list[dict[str, Any]]:
    sections = _split_transcript_for_translation(transcript)
    return [
        _build_transcript_section(section, label=f"Section {index}")
        for index, section in enumerate(sections, start=1)
    ]


def _coerce_transcript_sections(
    transcript: str,
    transcript_sections: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    transcript = transcript.strip()
    existing_sections = [
        _build_transcript_section(
            str(section.get("transcript", "")).strip(),
            section.get("start_seconds"),
            section.get("end_seconds"),
            section.get("label") or f"Section {index}",
        )
        for index, section in enumerate(transcript_sections or [], start=1)
        if str(section.get("transcript", "")).strip()
    ]

    if not transcript:
        return existing_sections
    if not existing_sections:
        return _fallback_transcript_sections(transcript)

    existing_joined = "\n\n".join(section["transcript"] for section in existing_sections).strip()
    if normalize_text(existing_joined) == normalize_text(transcript):
        return existing_sections

    blocks = [block.strip() for block in re.split(r"\n{2,}", transcript) if block.strip()]
    if len(blocks) == len(existing_sections):
        return [
            _build_transcript_section(
                blocks[index - 1],
                section.get("start_seconds"),
                section.get("end_seconds"),
                section.get("label") or f"Section {index}",
            )
            for index, section in enumerate(existing_sections, start=1)
        ]

    redistributed = _split_text_into_count(transcript, len(existing_sections))
    if len(redistributed) == len(existing_sections):
        return [
            _build_transcript_section(
                redistributed[index - 1],
                section.get("start_seconds"),
                section.get("end_seconds"),
                section.get("label") or f"Section {index}",
            )
            for index, section in enumerate(existing_sections, start=1)
        ]

    return _fallback_transcript_sections(transcript)


def _fallback_meeting_gist_for_section(
    section: dict[str, Any],
    glossary_hits: list[dict[str, Any]],
    memory_hits: list[dict[str, Any]],
    translation_context: str = "",
    section_index: int = 1,
) -> dict[str, Any]:
    glossary_terms = [f"{hit['navajo_term']} = {hit['english_meaning']}" for hit in glossary_hits[:3]]
    memory_notes = [hit["final_translation"] for hit in memory_hits[:2] if hit.get("final_translation")]
    detail_lines = glossary_terms + memory_notes
    if translation_context.strip():
        detail_lines.append(f"Context: {translation_context.strip()}")

    return {
        "label": section.get("label") or f"Section {section_index}",
        "start_seconds": section.get("start_seconds"),
        "end_seconds": section.get("end_seconds"),
        "headline": f"Possible discussion in {section.get('label') or f'Section {section_index}'}",
        "gist": "AI meeting notes are unavailable. Use the audio playback, context, and glossary to review this section manually.",
        "important_details": detail_lines[:4],
        "confidence": "low",
        "draft_explanation": "Built from saved context because AI meeting-note drafting was unavailable.",
        "possible_reference": "",
        "guess_reason": "",
        "is_inference": False,
        "transcript": section.get("transcript", ""),
    }


def _build_meeting_gist_for_section(
    section: dict[str, Any],
    glossary_hits: list[dict[str, Any]],
    memory_hits: list[dict[str, Any]],
    translation_context: str = "",
) -> dict[str, Any]:
    transcript = str(section.get("transcript", "")).strip()
    if not settings.openai_configured or not transcript:
        return _fallback_meeting_gist_for_section(section, glossary_hits, memory_hits, translation_context)

    glossary_block = json.dumps(glossary_hits, ensure_ascii=False, indent=2)
    memory_block = json.dumps(memory_hits, ensure_ascii=False, indent=2)
    context_block = translation_context.strip() or "No extra context provided."
    section_label = section.get("label") or "this transcript section"

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.responses.create(
            model=settings.translation_model,
            instructions=(
                "You help a reviewer understand a Navajo community meeting. "
                "Do not give a literal word-for-word translation. "
                "Instead, provide a short plain-English meeting note about what this section is probably about. "
                "Use uncertain language when needed, and never invent certainty. "
                "You may connect multiple clues into a bigger-picture guess, such as a known story, event, or topic. "
                "If you do that, clearly mark it as a guess, not a fact, and explain which clues support the guess. "
                "Pull out likely names, places, numbers, requests, or decisions if they seem present. "
                "Return valid JSON only."
            ),
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                f"Create a meeting-understanding note for {section_label}.\n\n"
                                f"Phonetic transcript section:\n{transcript}\n\n"
                                f"Context from the user:\n{context_block}\n\n"
                                f"Glossary hits:\n{glossary_block}\n\n"
                                f"Approved examples:\n{memory_block}\n\n"
                                "Return JSON with keys: headline, gist, important_details, confidence, draft_explanation, possible_reference, guess_reason, is_inference. "
                                "headline should be 3-8 words. gist should be 1-3 short sentences. "
                                "important_details should be an array of up to 4 short strings. "
                                "If several clues point to a known story, event, or topic, set is_inference to true, put the short guessed reference in possible_reference, "
                                "and explain the clue chain in guess_reason. Use wording like 'possible reference' or 'this may be about'. "
                                "If there is no strong higher-level guess, set is_inference to false and leave possible_reference and guess_reason empty. "
                                "Confidence must be one of: low, medium, high."
                            ),
                        }
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "meeting_section_note",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "headline": {"type": "string"},
                            "gist": {"type": "string"},
                            "important_details": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                            "draft_explanation": {"type": "string"},
                            "possible_reference": {"type": "string"},
                            "guess_reason": {"type": "string"},
                            "is_inference": {"type": "boolean"},
                        },
                        "required": [
                            "headline",
                            "gist",
                            "important_details",
                            "confidence",
                            "draft_explanation",
                            "possible_reference",
                            "guess_reason",
                            "is_inference",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
        )

        payload = json.loads(getattr(response, "output_text", "").strip() or "{}")
        if not payload:
            return _fallback_meeting_gist_for_section(section, glossary_hits, memory_hits, translation_context)
        return {
            "label": section_label,
            "start_seconds": section.get("start_seconds"),
            "end_seconds": section.get("end_seconds"),
            "headline": payload.get("headline", "").strip() or f"Possible discussion in {section_label}",
            "gist": payload.get("gist", "").strip(),
            "important_details": [str(item).strip() for item in payload.get("important_details", []) if str(item).strip()][:4],
            "confidence": payload.get("confidence", "low").strip() or "low",
            "draft_explanation": payload.get("draft_explanation", "").strip(),
            "possible_reference": payload.get("possible_reference", "").strip(),
            "guess_reason": payload.get("guess_reason", "").strip(),
            "is_inference": bool(payload.get("is_inference", False)),
            "transcript": transcript,
        }
    except Exception as exc:
        draft = _fallback_meeting_gist_for_section(section, glossary_hits, memory_hits, translation_context)
        draft["draft_explanation"] = f"{draft['draft_explanation']} AI meeting-note draft failed: {exc}"
        return draft


def _fallback_meeting_summary(
    meeting_gist: list[dict[str, Any]],
    translation_context: str = "",
    glossary_hits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    glossary_hits = glossary_hits or []
    main_topics = [item.get("headline", "").strip() for item in meeting_gist if item.get("headline")]
    big_picture_guesses: list[str] = []
    seen_guesses: set[str] = set()
    for item in meeting_gist:
        guessed_reference = str(item.get("possible_reference", "")).strip()
        if not guessed_reference:
            continue
        normalized = normalize_text(guessed_reference)
        if not normalized or normalized in seen_guesses:
            continue
        seen_guesses.add(normalized)
        reason = str(item.get("guess_reason", "")).strip()
        guess_line = f"Possible reference: {guessed_reference}"
        if reason:
            guess_line = f"{guess_line}. Guess based on: {reason}"
        big_picture_guesses.append(guess_line)
    names_numbers = [
        f"{hit['navajo_term']} = {hit['english_meaning']}"
        for hit in glossary_hits[:4]
        if hit.get("navajo_term") and hit.get("english_meaning")
    ]

    overall_takeaway = (
        "These notes are a rough guide. Use the timeline cards and audio playback to confirm meaning."
        if meeting_gist
        else "No meeting notes are available yet. Add or correct the transcript, then refresh the notes."
    )
    if translation_context.strip():
        overall_takeaway = f"{overall_takeaway} Context provided: {translation_context.strip()}."

    return {
        "main_topics": main_topics[:4],
        "big_picture_guesses": big_picture_guesses[:4],
        "concerns_requests": [],
        "decisions_actions": [],
        "names_numbers": names_numbers[:4],
        "overall_takeaway": overall_takeaway,
    }


def _build_meeting_summary(
    meeting_gist: list[dict[str, Any]],
    translation_context: str = "",
    glossary_hits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    glossary_hits = glossary_hits or []
    if not settings.openai_configured or not meeting_gist:
        return _fallback_meeting_summary(meeting_gist, translation_context, glossary_hits)

    note_block = json.dumps(
        [
            {
                "label": item.get("label", ""),
                "headline": item.get("headline", ""),
                "gist": item.get("gist", ""),
                "important_details": item.get("important_details", []),
                "confidence": item.get("confidence", "low"),
                "possible_reference": item.get("possible_reference", ""),
                "guess_reason": item.get("guess_reason", ""),
                "is_inference": item.get("is_inference", False),
            }
            for item in meeting_gist
        ],
        ensure_ascii=False,
        indent=2,
    )
    glossary_block = json.dumps(glossary_hits[:8], ensure_ascii=False, indent=2)
    context_block = translation_context.strip() or "No extra context provided."

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.responses.create(
            model=settings.translation_model,
            instructions=(
                "You summarize likely meaning from meeting-understanding notes. "
                "Do not overstate certainty. Keep the output practical for a reviewer who wants to know what the meeting is probably about. "
                "If the section notes imply a bigger-picture story, event, or topic, list it as a guess and never as a confirmed fact."
            ),
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Summarize the meeting notes below.\n\n"
                                f"Context from the user:\n{context_block}\n\n"
                                f"Glossary hits:\n{glossary_block}\n\n"
                                f"Section notes:\n{note_block}\n\n"
                                "Return JSON with keys: main_topics, big_picture_guesses, concerns_requests, decisions_actions, names_numbers, overall_takeaway. "
                                "Each list should contain short strings. Keep overall_takeaway to 1-2 sentences. "
                                "Any higher-level story or topic inference must be labeled as a guess."
                            ),
                        }
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "meeting_summary",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "main_topics": {"type": "array", "items": {"type": "string"}},
                            "big_picture_guesses": {"type": "array", "items": {"type": "string"}},
                            "concerns_requests": {"type": "array", "items": {"type": "string"}},
                            "decisions_actions": {"type": "array", "items": {"type": "string"}},
                            "names_numbers": {"type": "array", "items": {"type": "string"}},
                            "overall_takeaway": {"type": "string"},
                        },
                        "required": [
                            "main_topics",
                            "big_picture_guesses",
                            "concerns_requests",
                            "decisions_actions",
                            "names_numbers",
                            "overall_takeaway",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
        )

        payload = json.loads(getattr(response, "output_text", "").strip() or "{}")
        if not payload:
            return _fallback_meeting_summary(meeting_gist, translation_context, glossary_hits)
        return {
            "main_topics": [str(item).strip() for item in payload.get("main_topics", []) if str(item).strip()][:5],
            "big_picture_guesses": [str(item).strip() for item in payload.get("big_picture_guesses", []) if str(item).strip()][:5],
            "concerns_requests": [str(item).strip() for item in payload.get("concerns_requests", []) if str(item).strip()][:5],
            "decisions_actions": [str(item).strip() for item in payload.get("decisions_actions", []) if str(item).strip()][:5],
            "names_numbers": [str(item).strip() for item in payload.get("names_numbers", []) if str(item).strip()][:5],
            "overall_takeaway": payload.get("overall_takeaway", "").strip(),
        }
    except Exception:
        return _fallback_meeting_summary(meeting_gist, translation_context, glossary_hits)


def _compose_meeting_notes_text(meeting_summary: dict[str, Any], meeting_gist: list[dict[str, Any]]) -> str:
    lines: list[str] = []

    overall_takeaway = str(meeting_summary.get("overall_takeaway", "")).strip()
    if overall_takeaway:
        lines.append(f"Overall sense: {overall_takeaway}")

    section_map = [
        ("Main topics", meeting_summary.get("main_topics", [])),
        ("Possible bigger-picture guesses", meeting_summary.get("big_picture_guesses", [])),
        ("Concerns or requests", meeting_summary.get("concerns_requests", [])),
        ("Decisions or actions", meeting_summary.get("decisions_actions", [])),
        ("Names, places, or numbers", meeting_summary.get("names_numbers", [])),
    ]
    for heading, items in section_map:
        clean_items = [str(item).strip() for item in items if str(item).strip()]
        if not clean_items:
            continue
        if lines:
            lines.append("")
        lines.append(f"{heading}:")
        lines.extend(f"- {item}" for item in clean_items[:5])

    if meeting_gist:
        if lines:
            lines.append("")
        lines.append("Timeline notes:")
        for item in meeting_gist:
            label = item.get("label") or "Section"
            gist = str(item.get("gist", "")).strip()
            details = [str(detail).strip() for detail in item.get("important_details", []) if str(detail).strip()]
            summary_line = f"- {label}: {gist}".strip()
            lines.append(summary_line)
            possible_reference = str(item.get("possible_reference", "")).strip()
            guess_reason = str(item.get("guess_reason", "")).strip()
            if possible_reference:
                guess_line = f"  Guess: possible reference is {possible_reference}"
                if guess_reason:
                    guess_line = f"{guess_line}. Why guess: {guess_reason}"
                lines.append(guess_line)
            if details:
                lines.append(f"  Details: {'; '.join(details[:3])}")

    return "\n".join(lines).strip()[:4500]


def fallback_translation_draft(
    transcript: str,
    glossary_hits: list[dict[str, Any]],
    memory_hits: list[dict[str, Any]],
    translation_context: str = "",
    transcript_sections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    review_sections = _coerce_transcript_sections(transcript, transcript_sections)
    if not transcript.strip():
        return {
            "draft_translation": "",
            "confidence": "low",
            "draft_explanation": "No transcript is available yet. Add or correct the Navajo transcript, then refresh the ideas.",
            "transcript_sections": review_sections,
            "meeting_gist": [],
            "meeting_summary": {},
        }

    gist_entries = [
        _fallback_meeting_gist_for_section(
            section,
            _filter_glossary_hits_for_section(section.get("transcript", ""), glossary_hits),
            _filter_memory_hits_for_section(section.get("transcript", ""), memory_hits),
            translation_context,
            section_index=index,
        )
        for index, section in enumerate(review_sections, start=1)
    ]
    meeting_summary = _fallback_meeting_summary(gist_entries, translation_context, glossary_hits)
    explanation = "AI meeting-note drafting is unavailable, so these notes were built from saved context and project memory."
    return {
        "draft_translation": _compose_meeting_notes_text(meeting_summary, gist_entries),
        "confidence": "low",
        "draft_explanation": explanation,
        "transcript_sections": review_sections,
        "meeting_gist": gist_entries,
        "meeting_summary": meeting_summary,
    }


def build_translation_draft(
    transcript: str,
    glossary_hits: list[dict[str, Any]],
    memory_hits: list[dict[str, Any]],
    translation_context: str = "",
    transcript_sections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not settings.openai_configured or not transcript.strip():
        return fallback_translation_draft(
            transcript,
            glossary_hits,
            memory_hits,
            translation_context,
            transcript_sections=transcript_sections,
        )

    sections = _coerce_transcript_sections(transcript, transcript_sections)
    if not sections:
        return fallback_translation_draft(
            transcript,
            glossary_hits,
            memory_hits,
            translation_context,
            transcript_sections=transcript_sections,
        )

    meeting_gist: list[dict[str, Any]] = []
    section_explanations: list[str] = []
    confidences: list[str] = []

    for index, section in enumerate(sections, start=1):
        section_text = section.get("transcript", "")
        section_glossary_hits = _filter_glossary_hits_for_section(section_text, glossary_hits)
        section_memory_hits = _filter_memory_hits_for_section(section_text, memory_hits)
        draft = _build_meeting_gist_for_section(
            section,
            section_glossary_hits,
            section_memory_hits,
            translation_context,
        )
        meeting_gist.append(draft)
        section_explanations.append(f"{draft['label']}: {draft['draft_explanation'].strip()}")
        confidences.append(draft.get("confidence", "low").strip() or "low")

    meeting_summary = _build_meeting_summary(meeting_gist, translation_context, glossary_hits)
    return {
        "draft_translation": _compose_meeting_notes_text(meeting_summary, meeting_gist),
        "confidence": _combine_confidence_levels(confidences),
        "draft_explanation": (
            f"Meeting-understanding notes were generated across {len(sections)} transcript sections.\n\n"
            + "\n".join(section_explanations)
        ).strip(),
        "transcript_sections": sections,
        "meeting_gist": meeting_gist,
        "meeting_summary": meeting_summary,
    }
