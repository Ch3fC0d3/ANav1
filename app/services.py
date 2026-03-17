from __future__ import annotations

from contextlib import contextmanager
import json
import re
import shutil
import subprocess
import unicodedata
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
    terms: list[str] = []
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
    if settings.transcription_language:
        request_kwargs["language"] = settings.transcription_language
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


@contextmanager
def _prepare_transcription_chunks(audio_path: Path, chunk_seconds: int | None = None):
    chunk_seconds = max(30, chunk_seconds or settings.transcription_chunk_seconds)
    ffmpeg_path = _resolve_ffmpeg_executable()

    if not ffmpeg_path:
        yield [(audio_path, 0.0)], []
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
            yield [(audio_path, 0.0)], [warning]
            return

        chunk_paths = sorted(temp_dir.glob("chunk-*.wav"))
        if not chunk_paths:
            yield [(audio_path, 0.0)], ["Chunking produced no audio segments, so the file was sent as one transcription request."]
            return

        warnings: list[str] = []
        if len(chunk_paths) > 1:
            warnings.append(
                f"Long audio was split into {len(chunk_paths)} chunks of about {chunk_seconds} seconds each before transcription."
            )
        yield [(chunk_path, index * chunk_seconds) for index, chunk_path in enumerate(chunk_paths)], warnings
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _transcribe_chunk_with_fallback(
    client: OpenAI,
    chunk_path: Path,
    glossary_entries: list[dict[str, Any]],
    request_kwargs: dict[str, Any],
    time_offset_seconds: float,
    chunk_seconds: int,
    label: str,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
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
            chunk_seconds,
            f"{label} could not be transcribed: {exc}",
            label,
        )

    raw_text = (getattr(transcript, "text", "") or "").strip()
    phonetic_text = rewrite_as_english_letter_phonetics(raw_text, glossary_entries)
    if phonetic_text:
        return [phonetic_text], _extract_transcript_words(transcript, time_offset_seconds), []

    return _retry_chunk_with_smaller_segments(
        client,
        chunk_path,
        glossary_entries,
        request_kwargs,
        time_offset_seconds,
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
    chunk_seconds: int,
    primary_warning: str,
    label: str,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
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
        transcript_parts: list[str] = []
        words: list[dict[str, Any]] = []

        for retry_index, (retry_chunk_path, retry_offset_seconds) in enumerate(retry_chunk_list, start=1):
            nested_label = f"{label}.{retry_index}"
            retry_texts, retry_words, nested_warnings = _transcribe_chunk_with_fallback(
                client,
                retry_chunk_path,
                glossary_entries,
                request_kwargs,
                time_offset_seconds + retry_offset_seconds,
                next_chunk_seconds,
                nested_label,
            )
            transcript_parts.extend(retry_texts)
            words.extend(retry_words)
            warnings.extend(nested_warnings)

        if transcript_parts:
            return transcript_parts, words, warnings

        return [], words, warnings


def transcribe_audio(audio_path: Path, glossary_entries: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], list[str]]:
    if not settings.openai_configured:
        return "", [], ["OpenAI API key not configured. Add the transcript manually, then refresh the draft translation."]

    client = OpenAI(api_key=settings.openai_api_key)
    request_kwargs = _build_transcription_request_kwargs(glossary_entries)
    warnings: list[str] = []
    transcript_chunks: list[str] = []
    words: list[dict[str, Any]] = []

    top_level_chunk_seconds = max(30, settings.transcription_chunk_seconds)
    with _prepare_transcription_chunks(audio_path, top_level_chunk_seconds) as (audio_chunks, chunk_warnings):
        warnings.extend(chunk_warnings)

        for chunk_index, (chunk_path, time_offset_seconds) in enumerate(audio_chunks, start=1):
            chunk_texts, chunk_words, chunk_warnings = _transcribe_chunk_with_fallback(
                client,
                chunk_path,
                glossary_entries,
                request_kwargs,
                time_offset_seconds,
                top_level_chunk_seconds,
                f"Chunk {chunk_index}",
            )
            transcript_chunks.extend(chunk_texts)
            words.extend(chunk_words)
            warnings.extend(chunk_warnings)

    if not transcript_chunks:
        return "", words, warnings or ["Automatic transcription failed for every audio chunk."]

    return "\n\n".join(transcript_chunks), words, warnings


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


def _build_translation_draft_single(
    transcript: str,
    glossary_hits: list[dict[str, Any]],
    memory_hits: list[dict[str, Any]],
    translation_context: str = "",
    section_label: str = "",
) -> dict[str, str]:
    if not settings.openai_configured or not transcript.strip():
        return fallback_translation_draft(transcript, glossary_hits, memory_hits, translation_context)

    glossary_block = json.dumps(glossary_hits, ensure_ascii=False, indent=2)
    memory_block = json.dumps(memory_hits, ensure_ascii=False, indent=2)
    context_block = translation_context.strip() or "No extra context provided."
    scoped_label = section_label or "this transcript section"

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.responses.create(
            model=settings.translation_model,
            instructions=(
                "You assist with Navajo-to-English translation review. "
                "The user does not want an exact final translation. "
                "They want possible meanings, working interpretations, and idea sketches. "
                "Use the provided context, glossary, and approved examples as hints. "
                "Be explicit about uncertainty, avoid overclaiming, and never invent certainty. "
                "Cover the full section you are given, not just the opening sentence. "
                "Return valid JSON only."
            ),
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                f"Create draft English meaning ideas for {scoped_label} below.\n\n"
                                f"Transcript section:\n{transcript}\n\n"
                                f"Context from the user:\n{context_block}\n\n"
                                f"Glossary hits:\n{glossary_block}\n\n"
                                f"Approved examples:\n{memory_block}\n\n"
                                "Return JSON with keys: draft_translation, confidence, draft_explanation. "
                                "Make draft_translation cover the full section in 2-5 short lines or sentences. "
                                "These should be possible meanings or translation ideas, not a final answer. "
                                "Confidence must be one of: low, medium, high."
                            ),
                        }
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "translation_assist",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "draft_translation": {"type": "string"},
                            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                            "draft_explanation": {"type": "string"},
                        },
                        "required": ["draft_translation", "confidence", "draft_explanation"],
                        "additionalProperties": False,
                    },
                }
            },
        )

        payload = json.loads(getattr(response, "output_text", "").strip() or "{}")
        if not payload:
            return fallback_translation_draft(transcript, glossary_hits, memory_hits, translation_context)
        return {
            "draft_translation": payload.get("draft_translation", "").strip(),
            "confidence": payload.get("confidence", "low").strip() or "low",
            "draft_explanation": payload.get("draft_explanation", "").strip(),
        }
    except Exception as exc:
        draft = fallback_translation_draft(transcript, glossary_hits, memory_hits, translation_context)
        draft["draft_explanation"] = f"{draft['draft_explanation']} AI draft failed: {exc}"
        return draft


def fallback_translation_draft(
    transcript: str,
    glossary_hits: list[dict[str, Any]],
    memory_hits: list[dict[str, Any]],
    translation_context: str = "",
) -> dict[str, str]:
    if not transcript.strip():
        return {
            "draft_translation": "",
            "confidence": "low",
            "draft_explanation": "No transcript is available yet. Add or correct the Navajo transcript, then refresh the ideas.",
        }

    glossary_summary = ", ".join(f"{hit['navajo_term']} = {hit['english_meaning']}" for hit in glossary_hits[:4])
    memory_summary = "; ".join(hit["final_translation"] for hit in memory_hits[:2] if hit.get("final_translation"))
    parts = []
    if glossary_summary:
        parts.append(f"Possible key terms: {glossary_summary}.")
    if memory_summary:
        parts.append(f"Similar approved meanings: {memory_summary}.")
    if translation_context.strip():
        parts.append(f"User-provided context: {translation_context.strip()}.")
    explanation = " ".join(parts) if parts else "AI drafting is unavailable, so this is a placeholder built from saved project memory."
    return {
        "draft_translation": "Idea draft unavailable. Use the context, glossary, and saved examples to sketch possible meanings.",
        "confidence": "low",
        "draft_explanation": explanation,
    }


def build_translation_draft(
    transcript: str,
    glossary_hits: list[dict[str, Any]],
    memory_hits: list[dict[str, Any]],
    translation_context: str = "",
) -> dict[str, str]:
    if not settings.openai_configured or not transcript.strip():
        return fallback_translation_draft(transcript, glossary_hits, memory_hits, translation_context)

    sections = _split_transcript_for_translation(transcript)
    if len(sections) <= 1:
        return _build_translation_draft_single(
            transcript,
            glossary_hits,
            memory_hits,
            translation_context,
            section_label="this transcript",
        )

    section_outputs: list[str] = []
    section_explanations: list[str] = []
    confidences: list[str] = []

    for index, section in enumerate(sections, start=1):
        section_glossary_hits = _filter_glossary_hits_for_section(section, glossary_hits)
        section_memory_hits = _filter_memory_hits_for_section(section, memory_hits)
        draft = _build_translation_draft_single(
            section,
            section_glossary_hits,
            section_memory_hits,
            translation_context,
            section_label=f"section {index} of {len(sections)}",
        )
        section_outputs.append(f"Section {index}\n{draft['draft_translation'].strip()}")
        section_explanations.append(f"Section {index}: {draft['draft_explanation'].strip()}")
        confidences.append(draft.get("confidence", "low").strip() or "low")

    return {
        "draft_translation": "\n\n".join(section_outputs).strip(),
        "confidence": _combine_confidence_levels(confidences),
        "draft_explanation": (
            f"Draft ideas were generated section by section across {len(sections)} transcript sections.\n\n"
            + "\n".join(section_explanations)
        ).strip(),
    }
