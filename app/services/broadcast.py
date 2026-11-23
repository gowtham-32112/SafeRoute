"""SafeRoute Broadcast Announcement Service.

Allows administrators to publish system-wide civic notices (e.g. road works,
weather warnings, emergency advisories) that display as an alert banner to all
commuters and users across the application.
"""

from datetime import datetime, timezone
import logging
from typing import Literal, Optional
import uuid

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

BroadcastSeverity = Literal["info", "warning", "critical"]


class BroadcastMessage(BaseModel):
    """An active system broadcast announcement."""

    id: str
    title: str
    message: str
    severity: BroadcastSeverity = "info"
    created_by: str
    created_at: str
    is_active: bool = True


# In-memory store for active broadcast (stateless and fast across requests)
_ACTIVE_BROADCAST: Optional[BroadcastMessage] = None


def get_active_broadcast() -> Optional[BroadcastMessage]:
    """Retrieve the currently active broadcast message, if any."""
    global _ACTIVE_BROADCAST
    if _ACTIVE_BROADCAST and _ACTIVE_BROADCAST.is_active:
        return _ACTIVE_BROADCAST
    return None


def set_broadcast(
    title: str,
    message: str,
    severity: BroadcastSeverity = "info",
    created_by: str = "Municipal Admin",
) -> BroadcastMessage:
    """Publish a new system-wide broadcast message."""
    global _ACTIVE_BROADCAST
    now_iso = datetime.now(timezone.utc).isoformat()
    broadcast = BroadcastMessage(
        id=str(uuid.uuid4()),
        title=title.strip(),
        message=message.strip(),
        severity=severity,
        created_by=created_by.strip(),
        created_at=now_iso,
        is_active=True,
    )
    _ACTIVE_BROADCAST = broadcast
    logger.info("New system broadcast published by %s: %s", created_by, title)
    return broadcast


def clear_broadcast() -> None:
    """Clear or deactivate the current system broadcast message."""
    global _ACTIVE_BROADCAST
    _ACTIVE_BROADCAST = None
    logger.info("System broadcast dismissed/cleared.")
