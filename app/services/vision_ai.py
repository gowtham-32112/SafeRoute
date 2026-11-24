"""SafeRoute Stage 8 — Vision AI Category Suggestion Service (C1 / Issue #28).

Features:
- Validates evidence image format (JPEG, PNG, WebP) and 5 MB size limit.
- Strips 100% of EXIF/GPS/XMP metadata prior to transmission to protect user privacy.
- Calls Gemini Vision API with structured JSON output schema and short timeout.
- Normalizes and validates output strictly against approved categories:
  pothole, road_break, streetlight, footpath, other (or null).
- Guaranteed non-blocking fallback on any error, timeout, missing API key,
  or unapproved category -> returns category: null with HTTP 200 (never raises 500).
"""

import base64
import json
import logging
import re
from typing import Literal, Optional

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.services.images import (
    ProcessedImage,
    validate_and_strip_exif,
)

logger = logging.getLogger(__name__)

APPROVED_CATEGORIES = frozenset(
    {"pothole", "road_break", "streetlight", "footpath", "other"}
)

CATEGORY_SYNONYMS = {
    "pothole": "pothole",
    "potholes": "pothole",
    "crater": "pothole",
    "road_break": "road_break",
    "road break": "road_break",
    "road-break": "road_break",
    "crack": "road_break",
    "fissure": "road_break",
    "shoulder collapse": "road_break",
    "streetlight": "streetlight",
    "street light": "streetlight",
    "street_light": "streetlight",
    "light": "streetlight",
    "footpath": "footpath",
    "sidewalk": "footpath",
    "pavement": "footpath",
    "other": "other",
}

GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class CategorySuggestionResponse(BaseModel):
    """Authoritative response returned by POST /reports/suggest-category."""

    category: Optional[
        Literal["pothole", "road_break", "streetlight", "footpath", "other"]
    ] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    reason: Optional[str] = None


def _normalize_category(raw_cat: Optional[str]) -> Optional[str]:
    """Normalize a raw category string to one of the approved categories, or None."""
    if not raw_cat:
        return None
    cleaned = str(raw_cat).strip().lower().replace("-", "_")
    if cleaned in APPROVED_CATEGORIES:
        return cleaned
    return CATEGORY_SYNONYMS.get(cleaned)


def _clean_reason_text(raw_reason: Optional[str]) -> Optional[str]:
    """Sanitize reason to a single-line string capped at 160 characters."""
    if not raw_reason:
        return None
    cleaned = re.sub(r"[\r\n\t]+", " ", str(raw_reason)).strip()
    return cleaned[:160] if cleaned else None


def _extract_json_payload(text: str) -> Optional[dict]:
    """Extract and parse a JSON dictionary from model output text."""
    if not text or not text.strip():
        return None

    # First attempt: direct json.loads
    stripped = text.strip()
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    # Second attempt: extract from markdown code fences ```json ... ```
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    # Third attempt: first '{' to last '}'
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(stripped[start : end + 1])
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    return None


async def suggest_hazard_category(
    image_bytes: bytes,
    declared_content_type: Optional[str],
    app_settings: Settings,
) -> CategorySuggestionResponse:
    """Analyze a road hazard image and suggest a category.

    1. Validates and strips all EXIF/GPS metadata via Pillow.
    2. If API key is missing or blank, returns category: null gracefully.
    3. Calls Gemini Vision API via httpx.AsyncClient with strict JSON schema.
    4. On any timeout, network failure, or unapproved category, returns category: null.
    """
    # 1. Image validation & EXIF stripping (raises ImageValidationError on invalid size/format)
    processed: ProcessedImage = validate_and_strip_exif(
        image_bytes, declared_content_type
    )

    api_key = app_settings.active_vision_api_key
    if not api_key:
        logger.info("Vision AI suggestion skipped: active_vision_api_key is unconfigured.")
        return CategorySuggestionResponse(
            category=None,
            confidence=None,
            reason="AI category suggestion unavailable.",
        )

    # 2. Prepare Gemini Vision API request
    model_name = (app_settings.VISION_AI_MODEL or "gemini-2.5-flash").strip()
    endpoint = f"{GEMINI_API_BASE_URL}/{model_name}:generateContent"

    b64_image = base64.b64encode(processed.content).decode("ascii")

    prompt_text = (
        "You are an expert civil road-safety hazard classifier for SafeRoute.\n"
        "Analyze the attached photograph of a road or street and classify the primary hazard into "
        "strictly ONE of these 5 categories:\n"
        "- pothole: crater, depression, or cavity in the road surface\n"
        "- road_break: crack, asphalt fissure, broken pavement, collapsed road shoulder\n"
        "- streetlight: unlit street lamp, broken fixture, damaged pole, exposed wires\n"
        "- footpath: broken sidewalk, damaged paver tiles, missing curbstone, blocked walkway\n"
        "- other: other roadway hazards (e.g., uncovered manhole, water accumulation, tree debris)\n"
        "- none: if the image does not show any road hazard or is completely unreadable\n\n"
        "Return ONLY a strictly valid JSON object matching this schema:\n"
        "{\n"
        '  "category": "pothole" | "road_break" | "streetlight" | "footpath" | "other" | "none",\n'
        '  "confidence": <float between 0.0 and 1.0>,\n'
        '  "reason": "<concise one-line explanation, maximum 160 characters>"\n'
        "}"
    )

    request_payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt_text},
                    {
                        "inlineData": {
                            "mimeType": processed.content_type,
                            "data": b64_image,
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    # 3. Call upstream Vision AI provider with bounded timeout and safe fallbacks
    timeout_seconds = max(1.0, float(app_settings.VISION_AI_TIMEOUT_SECONDS))

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(
                endpoint,
                json=request_payload,
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            )

        if resp.status_code != 200:
            logger.warning(
                "Vision AI provider returned non-200 HTTP status %d: %s",
                resp.status_code,
                resp.text[:200],
            )
            return CategorySuggestionResponse(
                category=None,
                confidence=None,
                reason="AI category suggestion unavailable.",
            )

        response_json = resp.json()
        candidates = response_json.get("candidates") or []
        if not candidates or not isinstance(candidates, list):
            return CategorySuggestionResponse(
                category=None,
                confidence=None,
                reason="AI category suggestion unavailable.",
            )

        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        if not parts or not isinstance(parts, list):
            return CategorySuggestionResponse(
                category=None,
                confidence=None,
                reason="AI category suggestion unavailable.",
            )

        raw_text = parts[0].get("text") or ""
        parsed = _extract_json_payload(raw_text)
        if not parsed:
            logger.warning("Failed to extract JSON from Vision AI response: %s", raw_text[:200])
            return CategorySuggestionResponse(
                category=None,
                confidence=None,
                reason="AI category suggestion unavailable.",
            )

        # 4. Strictly normalize and validate output
        raw_cat = parsed.get("category")
        validated_category = _normalize_category(raw_cat)

        # Clamp confidence to [0.0, 1.0]
        validated_confidence: Optional[float] = None
        try:
            if parsed.get("confidence") is not None:
                c_val = float(parsed["confidence"])
                validated_confidence = max(0.0, min(1.0, round(c_val, 2)))
        except (ValueError, TypeError):
            validated_confidence = None

        validated_reason = _clean_reason_text(parsed.get("reason"))

        # If category is not in the approved set, treat as no hazard
        if not validated_category:
            return CategorySuggestionResponse(
                category=None,
                confidence=None,
                reason=validated_reason or "No road hazard detected.",
            )

        return CategorySuggestionResponse(
            category=validated_category,  # type: ignore[arg-type]
            confidence=validated_confidence if validated_confidence is not None else 0.8,
            reason=validated_reason or f"Detected {validated_category.replace('_', ' ')}.",
        )

    except (httpx.TimeoutException, httpx.RequestError) as net_err:
        logger.warning("Vision AI network/timeout error: %s", type(net_err).__name__)
        return CategorySuggestionResponse(
            category=None,
            confidence=None,
            reason="AI category suggestion unavailable.",
        )
    except Exception as exc:
        logger.warning("Unexpected error during Vision AI processing: %s", type(exc).__name__)
        return CategorySuggestionResponse(
            category=None,
            confidence=None,
            reason="AI category suggestion unavailable.",
        )
