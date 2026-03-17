from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from openai import OpenAI

from .config import settings


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def tokenize(value: str) -> list[str]:
    return re.findall(r"[a-zA-Z']+", normalize_text(value))


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


def transcribe_audio(audio_path: Path, glossary_entries: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], list[str]]:
    if not settings.openai_configured:
        return "", [], ["OpenAI API key not configured. Add the transcript manually, then refresh the draft translation."]

    client = OpenAI(api_key=settings.openai_api_key)
    request_kwargs: dict[str, Any] = {
        "model": settings.transcription_model,
        "response_format": "verbose_json",
        "timestamp_granularities": ["word"],
    }
    prompt = _build_transcription_prompt(glossary_entries)
    if prompt:
        request_kwargs["prompt"] = prompt
    if settings.transcription_language:
        request_kwargs["language"] = settings.transcription_language

    try:
        with audio_path.open("rb") as audio_file:
            transcript = client.audio.transcriptions.create(file=audio_file, **request_kwargs)
    except Exception as exc:
        return "", [], [f"Automatic transcription failed: {exc}"]

    words = []
    for item in getattr(transcript, "words", []) or []:
        words.append(
            {
                "word": getattr(item, "word", ""),
                "start": getattr(item, "start", None),
                "end": getattr(item, "end", None),
            }
        )
    warnings: list[str] = []
    return (getattr(transcript, "text", "") or "").strip(), words, warnings


def fallback_translation_draft(
    transcript: str,
    glossary_hits: list[dict[str, Any]],
    memory_hits: list[dict[str, Any]],
) -> dict[str, str]:
    if not transcript.strip():
        return {
            "draft_translation": "",
            "confidence": "low",
            "draft_explanation": "No transcript is available yet. Add or correct the Navajo transcript, then refresh the draft.",
        }

    glossary_summary = ", ".join(f"{hit['navajo_term']} = {hit['english_meaning']}" for hit in glossary_hits[:4])
    memory_summary = "; ".join(hit["final_translation"] for hit in memory_hits[:2] if hit.get("final_translation"))
    parts = []
    if glossary_summary:
        parts.append(f"Possible key terms: {glossary_summary}.")
    if memory_summary:
        parts.append(f"Similar approved meanings: {memory_summary}.")
    explanation = " ".join(parts) if parts else "AI drafting is unavailable, so this is a placeholder built from saved project memory."
    return {
        "draft_translation": "Draft unavailable. Use the glossary and saved examples to write the first English pass.",
        "confidence": "low",
        "draft_explanation": explanation,
    }


def build_translation_draft(
    transcript: str,
    glossary_hits: list[dict[str, Any]],
    memory_hits: list[dict[str, Any]],
) -> dict[str, str]:
    if not settings.openai_configured or not transcript.strip():
        return fallback_translation_draft(transcript, glossary_hits, memory_hits)

    glossary_block = json.dumps(glossary_hits, ensure_ascii=False, indent=2)
    memory_block = json.dumps(memory_hits, ensure_ascii=False, indent=2)

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.responses.create(
            model=settings.translation_model,
            instructions=(
                "You assist with Navajo-to-English translation review. "
                "You are not the final authority. Use the glossary and approved examples as hints, "
                "be explicit about uncertainty, and never invent certainty. "
                "Return valid JSON only."
            ),
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Create a draft English translation for the Navajo transcript below.\n\n"
                                f"Transcript:\n{transcript}\n\n"
                                f"Glossary hits:\n{glossary_block}\n\n"
                                f"Approved examples:\n{memory_block}\n\n"
                                "Return JSON with keys: draft_translation, confidence, draft_explanation. "
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
            return fallback_translation_draft(transcript, glossary_hits, memory_hits)
        return {
            "draft_translation": payload.get("draft_translation", "").strip(),
            "confidence": payload.get("confidence", "low").strip() or "low",
            "draft_explanation": payload.get("draft_explanation", "").strip(),
        }
    except Exception as exc:
        draft = fallback_translation_draft(transcript, glossary_hits, memory_hits)
        draft["draft_explanation"] = f"{draft['draft_explanation']} AI draft failed: {exc}"
        return draft
