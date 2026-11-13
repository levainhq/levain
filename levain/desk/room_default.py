"""levain.desk.room_default — what ``levain serve --room DIR`` opens when the caller names no profile: the host profile.

Phill's signed ruling np-97fa3dd6 (a), 11-12: "RETIRE 'NO HANDS' FOR CHAT EVERYWHERE. The levain Room DEFAULT becomes
the flowConnect profile. Outward or irreversible acts sign through a cockpit card." So the room's product default is
the vendor's own default tool set under ``bypassPermissions`` (a shell, writes and web fetch, the reach the operator's
other chat surface has), with levain's own turn guard (:mod:`levain.desk.room_turn_guard`) as its one PreToolUse hook.
The no-shell room stays available as an explicit opt-in, :data:`CONFINED` (``--room-confined``), and is still what
``RoomSetting(profile=None)`` means at the launcher.

The guard is PINNED: :data:`ROOM_TURN_GUARD_SHA256` is the sha256 of the bytes this release ships, the profile's
:class:`~levain.desk.room_profile.HookSpec` carries it, and the profile is refused (here, and again by the launcher at
every open and on the bytes it copies for the child) when the file is missing, unreadable or holds other bytes. There is
no fallback: not to a room without the guard, and not silently to the confined room.

Levain names no host text here: no system prompt and no dials (model, effort, turns, budget); the vendor's defaults and
the setting's model apply. A host with its own prompt, dials or hooks builds its own :class:`RoomProfile` and passes it.
``setting_sources`` is the operator's user settings plus the project and local settings of the directory the operator
named as ``DIR``, as the operator's other chat surface reads them; the confined room keeps reading ``user`` only.
"""
from __future__ import annotations

from pathlib import Path

from levain.desk.room_profile import HookSpec, RoomProfile

__all__ = ["CONFINED", "GUARD_MATCHER", "GUARD_PATH", "ROOM_TURN_GUARD_SHA256", "default_host_profile"]

# The opt-in spelling of the confined (no-shell) room for ``_build_room_serve(profile=...)`` (``--room-confined``).
CONFINED = "confined"

GUARD_PATH = Path(__file__).resolve().parent / "room_turn_guard.py"
# the sha256 of room_turn_guard.py as this release ships it; a re-sync of the guard re-pins it here, on purpose
ROOM_TURN_GUARD_SHA256 = "f74b43b35d1458449b665db94415c41035f37fb93e1eb2b22595d267a66b680e"
# the tools the guard refuses by name, plus Bash: the hook is loaded for exactly these (the guard's own HOOK_MATCHER)
GUARD_MATCHER = "Bash|Agent|Task|CronCreate|Monitor|ScheduleWakeup|Workflow"
GUARD_TIMEOUT = 10
DEFAULT_SETTING_SOURCES = ("user", "project", "local")


def default_host_profile() -> RoomProfile:
    """The room's default profile: bypass, the vendor's default tools, levain's pinned turn guard as the one hook.
    Raises ValueError naming the guard file when it is missing, unreadable or not the pinned bytes."""
    try:
        hook = HookSpec(GUARD_MATCHER, str(GUARD_PATH), GUARD_TIMEOUT, sha256=ROOM_TURN_GUARD_SHA256)
    except ValueError as exc:
        raise ValueError(f"the room's turn guard cannot be used ({exc}); the room does not open without it. Reinstall "
                         f"levain, or pass --room-confined for the no-shell room") from exc
    return RoomProfile(name="host", permission_mode="bypassPermissions", setting_sources=DEFAULT_SETTING_SOURCES,
                       hooks=(hook,))
