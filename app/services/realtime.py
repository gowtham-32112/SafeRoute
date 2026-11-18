"""Server-side Supabase Realtime -> Server-Sent Events (SSE) bridge (Option R1).

Security guarantees:
- Each SSE stream is authenticated independently via the caller's validated
  Stage 5 session (HttpOnly cookie or Bearer header).
- Uses AsyncRealtimeClient (realtime==2.34.1) with SUPABASE_ANON_KEY and
  set_auth(user_jwt) so PostgreSQL RLS enforces the caller's permissions.
- Access tokens, refresh tokens, and service-role keys are NEVER sent to
  the browser or included in SSE payloads.
- Always unsubscribes channels and closes sockets cleanly on disconnect.
"""

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Callable, Optional
import uuid

from realtime import AsyncRealtimeClient, RealtimePostgresChangesListenEvent

from app.config import Settings, settings
from app.services.reports import parse_postgis_point, severity_band
from app.services.supabase_client import SupabaseConfigError

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_SECONDS = 25.0
MAX_QUEUE_SIZE = 100


def build_realtime_ws_url(supabase_url: str) -> str:
    """Convert an HTTPS/HTTP Supabase project URL to its Realtime WebSocket URL."""
    cleaned = supabase_url.strip().rstrip("/")
    if cleaned.startswith("https://"):
        return f"wss://{cleaned[len('https://'):]}/realtime/v1"
    if cleaned.startswith("http://"):
        return f"ws://{cleaned[len('http://'):]}/realtime/v1"
    raise SupabaseConfigError("SUPABASE_URL must begin with https:// or http://.")


def sanitize_hazard_event(payload: Any) -> Optional[dict[str, Any]]:
    """Extract only safe, non-sensitive hazard fields from a Realtime payload."""
    if not isinstance(payload, dict):
        return None

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    event_type = str(
        data.get("type")
        or data.get("eventType")
        or payload.get("eventType")
        or ""
    ).upper()
    if event_type not in ("INSERT", "UPDATE", "DELETE"):
        return None

    record = data.get("record") if isinstance(data.get("record"), dict) else {}
    old_record = data.get("old_record") if isinstance(data.get("old_record"), dict) else {}
    source = record if record else old_record
    report_id = source.get("id")
    if not report_id:
        return None

    lat: Optional[float] = None
    lng: Optional[float] = None
    if record.get("location") is not None:
        try:
            lat, lng = parse_postgis_point(record["location"])
        except Exception:
            lat, lng = None, None

    score = int(record.get("severity_score") or 0) if record else 0
    out: dict[str, Any] = {
        "event_type": event_type,
        "id": str(report_id),
    }
    if record:
        out["report"] = {
            "id": str(report_id),
            "category": str(record.get("category") or ""),
            "description": str(record.get("description") or ""),
            "lat": lat,
            "lng": lng,
            "address_text": record.get("address_text"),
            "status": str(record.get("status") or "open"),
            "escalation_recommended": bool(record.get("escalation_recommended", False)),
            "severity_score": score,
            "severity_band": severity_band(score),
            "confidence": str(record.get("confidence") or "low"),
            "confirm_count": int(record.get("confirm_count") or 0),
            "dispute_count": int(record.get("dispute_count") or 0),
            "fixed_count": int(record.get("fixed_count") or 0),
            "created_at": str(record.get("created_at") or ""),
            "updated_at": str(record.get("updated_at") or ""),
        }
    return out


def format_sse_message(event_name: str, data: dict[str, Any]) -> str:
    """Format a dictionary as an RFC-compliant Server-Sent Events frame."""
    return f"event: {event_name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def stream_hazard_updates(
    *,
    access_token: str,
    is_disconnected: Callable[[], Any],
    app_settings: Optional[Settings] = None,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    realtime_client_factory: Optional[Callable[..., Any]] = None,
) -> AsyncIterator[str]:
    """Yield SSE frames for authenticated public.hazard_reports Realtime events.

    Always closes the upstream Realtime WebSocket and removes channels on exit.
    """
    cfg = app_settings or settings
    if not cfg.is_supabase_configured:
        yield format_sse_message(
            "error",
            {"detail": "Realtime service is temporarily unavailable."},
        )
        return

    ws_url = build_realtime_ws_url(cfg.SUPABASE_URL)
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    factory = realtime_client_factory or AsyncRealtimeClient
    rt_client = factory(ws_url, cfg.SUPABASE_ANON_KEY.strip(), auto_reconnect=True)
    listen_task: Optional[asyncio.Task[Any]] = None

    def _on_change(payload: Any) -> None:
        sanitized = sanitize_hazard_event(payload)
        if sanitized is None:
            return
        try:
            queue.put_nowait(sanitized)
        except asyncio.QueueFull:
            # Drop oldest event to protect server memory under bursts
            try:
                queue.get_nowait()
                queue.put_nowait(sanitized)
            except Exception:
                pass

    try:
        await rt_client.connect()
        await rt_client.set_auth(access_token)
        channel_name = f"hazard-reports-{uuid.uuid4().hex[:12]}"
        channel = rt_client.channel(channel_name)
        channel.on_postgres_changes(
            event=RealtimePostgresChangesListenEvent.All,
            schema="public",
            table="hazard_reports",
            callback=_on_change,
        )
        await channel.subscribe()
        listen_task = asyncio.create_task(rt_client.listen())

        # Suggest client reconnect interval and confirm connected state
        yield "retry: 5000\n"
        yield format_sse_message("connected", {"status": "subscribed"})

        while True:
            if await is_disconnected():
                break
            if listen_task.done():
                yield format_sse_message(
                    "reconnect",
                    {"detail": "Realtime stream closed; reconnecting."},
                )
                break
            try:
                event_payload = await asyncio.wait_for(
                    queue.get(),
                    timeout=heartbeat_seconds,
                )
                yield format_sse_message("hazard_change", event_payload)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Realtime SSE stream error: %s", type(exc).__name__)
        yield format_sse_message(
            "error",
            {"detail": "Live hazard updates are temporarily unavailable."},
        )
    finally:
        if listen_task is not None and not listen_task.done():
            listen_task.cancel()
            try:
                await listen_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await rt_client.remove_all_channels()
        except Exception:
            pass
        try:
            await rt_client.close()
        except Exception:
            pass
