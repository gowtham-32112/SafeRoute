"""SafeRoute Commuter Civic AI Assistant Service.

Provides an intelligent conversational assistant for commuters to check live road
data, including how many hazards are reported, how many are resolved, category
breakdowns, and high-risk hotspots in Kukatpally.
"""

from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

from app.config import Settings, settings
from app.services.analytics import compute_analytics
from app.services.supabase_client import (
    create_authenticated_supabase_client,
    get_supabase_client,
)

logger = logging.getLogger(__name__)


class AiAssistantRequest(BaseModel):
    """User query payload for the commuter civic bot."""

    query: str = Field(..., min_length=1, max_length=500)


class AiAssistantResponse(BaseModel):
    """Civic bot answer with verified data context and follow-up chips."""

    reply: str
    suggested_actions: List[str] = []
    stats: Dict[str, Any] = {}


def _get_live_civic_context(
    access_token: str,
    app_settings: Optional[Settings] = None,
) -> Dict[str, Any]:
    """Retrieve verified aggregated database statistics to ground the AI bot."""
    cfg = app_settings or settings
    rows: list[dict[str, Any]] = []
    try:
        if access_token and access_token.strip():
            client = create_authenticated_supabase_client(access_token, cfg)
        else:
            client = get_supabase_client(cfg)

        resp = (
            client.table("hazard_reports")
            .select(
                "id, category, description, location, address_text, status, severity_score, "
                "confirm_count, dispute_count, fixed_count, duplicate_of, is_seed, created_at, fixed_at"
            )
            .execute()
        )
        data = getattr(resp, "data", None)
        if isinstance(data, list):
            rows = data
        elif isinstance(resp, dict) and isinstance(resp.get("data"), list):
            rows = resp["data"]
    except Exception as exc:
        logger.warning("Could not query live stats for AI assistant: %s", exc)

    analytics = compute_analytics(rows, exclude_seed=False)

    status_counts = analytics.by_status or {}
    open_count = status_counts.get("open", 0)
    escalated_count = status_counts.get("escalated", 0)
    fixed_count = status_counts.get("fixed", 0)
    total_count = analytics.total_reports

    cat_counts = analytics.by_category or {}
    top_areas = [f"{item.area_name} ({item.count} reports)" for item in (analytics.top_affected_areas or [])[:5]]

    return {
        "total_reports": total_count,
        "open_count": open_count,
        "escalated_count": escalated_count,
        "fixed_count": fixed_count,
        "resolution_rate_percent": round(
            (fixed_count / total_count * 100) if total_count > 0 else 0.0, 1
        ),
        "avg_time_to_fix_hours": round(analytics.avg_time_to_fix_hours or 0.0, 1),
        "category_breakdown": cat_counts,
        "top_affected_areas": top_areas,
    }


def _rule_based_civic_answer(query: str, ctx: Dict[str, Any]) -> tuple[str, List[str]]:
    """Synthesize accurate, direct answers from verified live data when offline or as fallback."""
    q = query.lower().strip()
    total = ctx["total_reports"]
    fixed = ctx["fixed_count"]
    opened = ctx["open_count"]
    escalated = ctx["escalated_count"]
    rate = ctx["resolution_rate_percent"]
    time_fix = ctx["avg_time_to_fix_hours"]
    categories = ctx["category_breakdown"]
    top_areas = ctx["top_affected_areas"]

    # 1. Questions about resolved / fixed issues
    if any(k in q for k in ("resolve", "fixed", "close", "solved")):
        reply = (
            f"**Resolution Status Report**:\n\n"
            f"- **{fixed} hazards** have been confirmed resolved ({rate}% resolution rate).\n"
            f"- The average time to resolve a hazard is **{time_fix} hours**.\n"
            f"- Currently, **{opened}** issues remain open and **{escalated}** have been escalated "
            f"to municipal authorities for priority maintenance."
        )
        suggestions = [
            "What issues are currently open?",
            "Show top hazard hotspots in Kukatpally",
            "How many potholes are reported?",
        ]
        return reply, suggestions

    # 2. Questions about potholes or specific categories
    if any(k in q for k in ("pothole", "break", "light", "footpath", "category")):
        potholes = categories.get("pothole", 0)
        breaks = categories.get("road_break", 0)
        lights = categories.get("streetlight", 0)
        footpaths = categories.get("footpath", 0)
        other = categories.get("other", 0)

        reply = (
            f"**Hazard Category Breakdown**:\n\n"
            f"- **Potholes**: {potholes} reported\n"
            f"- **Road Breaks & Cracks**: {breaks} reported\n"
            f"- **Streetlight Outages**: {lights} reported\n"
            f"- **Footpath Obstacles**: {footpaths} reported\n"
            f"- **Other Hazards**: {other} reported\n\n"
            f"Total hazards recorded in the system: **{total}**."
        )
        suggestions = [
            "How many hazards are resolved?",
            "Where are the most potholes located?",
            "What are the safest routes?",
        ]
        return reply, suggestions

    # 3. Questions about hotspots or locations in Kukatpally
    if any(k in q for k in ("hotspot", "area", "location", "kukatpally", "where", "jntu", "kphb")):
        areas_text = "\n".join([f"- {a}" for a in top_areas]) if top_areas else "- No hotspot data available"
        reply = (
            f"**Top Affected Areas in Kukatpally**:\n\n"
            f"{areas_text}\n\n"
            f"Commuters travelling through these corridors should exercise extra caution and consult the **Route Planner** (/routes) for hazard-aware alternatives."
        )
        suggestions = [
            "How many hazards have been resolved?",
            "Show hazard breakdown by category",
            "Plan a hazard-avoiding route",
        ]
        return reply, suggestions

    # 4. Default civic summary
    reply = (
        f"**SafeRoute Community Overview**:\n\n"
        f"- **Total Hazards**: {total} reported by commuters.\n"
        f"- **Active Issues**: {opened} open, {escalated} escalated to road authorities.\n"
        f"- **Resolved Issues**: {fixed} verified as fixed by the community ({rate}%).\n"
        f"- **Average Resolution Time**: {time_fix} hours.\n\n"
        f"Top affected corridor: **{top_areas[0] if top_areas else 'Kukatpally Y Junction'}**."
    )
    suggestions = [
        "How many hazards are resolved?",
        "What are the top hazard hotspots?",
        "Break down issues by category",
    ]
    return reply, suggestions


def answer_commuter_query(
    query: str,
    access_token: str,
    app_settings: Optional[Settings] = None,
) -> AiAssistantResponse:
    """Generate a verified, data-grounded response for commuter queries."""
    cfg = app_settings or settings
    ctx = _get_live_civic_context(access_token, cfg)

    # If Gemini API key is configured, synthesize via Gemini using the live stats
    active_key = cfg.active_vision_api_key
    if active_key and not active_key.startswith("demo_"):
        try:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{cfg.VISION_AI_MODEL}:generateContent"
            )
            prompt_text = (
                f"You are SafeRoute AI, a helpful community road-safety assistant for the Kukatpally area.\n"
                f"Ground your answer strictly in these real-time database statistics:\n"
                f"{json.dumps(ctx, indent=2)}\n\n"
                f"User Question: {query}\n\n"
                f"Instructions:\n"
                f"1. Answer concisely in friendly, professional civic tone.\n"
                f"2. Cite exact numbers from the data (e.g. resolved count, open count, top areas).\n"
                f"3. Never invent facts outside the provided data."
            )
            payload = {
                "contents": [{"parts": [{"text": prompt_text}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 300},
            }
            with httpx.Client(timeout=6.0) as client:
                res = client.post(
                    url,
                    json=payload,
                    headers={"x-goog-api-key": active_key},
                )
                if res.status_code == 200:
                    data = res.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        text_part = candidates[0].get("content", {}).get("parts", [{}])[0].get("text")
                        if text_part:
                            return AiAssistantResponse(
                                reply=text_part.strip(),
                                suggested_actions=[
                                    "How many hazards are resolved?",
                                    "Show top hazard hotspots",
                                    "Break down issues by category",
                                ],
                                stats=ctx,
                            )
        except Exception as exc:
            logger.warning("Gemini AI assistant fallback triggered: %s", exc)

    # Robust local rule-based civic response
    reply, suggestions = _rule_based_civic_answer(query, ctx)
    return AiAssistantResponse(
        reply=reply,
        suggested_actions=suggestions,
        stats=ctx,
    )
