"""Coordination so only one process talks to the node at a time.

A MeshCore companion session is effectively singular: messages are popped off
the device with SYNC_NEXT_MESSAGE, so two connected clients split the queue
between them. If the relay and the clock checker both fetch, the relay can
swallow a CLI reply the checker is waiting for, and the checker can swallow
channel messages that should have been relayed — silently, in both directions.

The handshake is three files in one directory:

* ``relay.pid``      — written by the relay while it runs, so others can tell
                       whether a relay is even present.
* ``mesh.pause``     — created by a tool that wants the node to itself.
* ``relay.released`` — written by the relay once it has actually disconnected
                       in response to that request. This is the part that makes
                       the handshake reliable: the caller waits for proof rather
                       than sleeping and hoping.

The relay already reconnects indefinitely, so releasing the node and picking it
back up afterwards needs no extra machinery.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("relay.coordination")

PID_NAME = "relay.pid"
PAUSE_NAME = "mesh.pause"
RELEASED_NAME = "relay.released"


class Coordinator:
    """Shared view of the three handshake files in a directory."""

    def __init__(self, directory: str | os.PathLike[str] = "."):
        self._dir = Path(directory or ".")

    @property
    def pause_file(self) -> Path:
        return self._dir / PAUSE_NAME

    @property
    def pid_file(self) -> Path:
        return self._dir / PID_NAME

    @property
    def released_file(self) -> Path:
        return self._dir / RELEASED_NAME

    # --- relay side -------------------------------------------------------

    def write_pid(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not write %s: %s", self.pid_file, exc)

    def clear_pid(self) -> None:
        _unlink(self.pid_file)

    def pause_requested(self) -> bool:
        return self.pause_file.exists()

    def mark_released(self) -> None:
        """Tell the waiting tool that the node is genuinely free."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self.released_file.write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not write %s: %s", self.released_file, exc)

    def clear_released(self) -> None:
        _unlink(self.released_file)

    # --- caller side ------------------------------------------------------

    def relay_pid(self) -> Optional[int]:
        """PID of a running relay, or None if there isn't one."""
        try:
            pid = int(self.pid_file.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError, OSError):
            return None
        return pid if _alive(pid) else None

    def request_pause(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self.pause_file.write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"could not create {self.pause_file}: {exc}")

    def release_request(self) -> None:
        _unlink(self.pause_file)

    def relay_has_released(self) -> bool:
        return self.released_file.exists()


def _alive(pid: int) -> bool:
    """Whether a process exists. Signal 0 checks without touching it."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not remove %s: %s", path, exc)
