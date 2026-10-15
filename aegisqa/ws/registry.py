"""Who is connected to which project, and how events reach them.

One room per project. Shared events (proposed test cases, run progress, ...)
go to everyone in the room; private ones (chat text, agent activity, errors)
only to one user's own sockets.

Rooms live in this process's memory: with more than one backend instance, a
user only receives events produced by the instance their socket is on. See
docs/GCP_HANDOFF.md (session affinity / single instance, or a Redis fan-out).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)

Emit = Callable[[dict[str, Any]], None]


class Connection:
  """A socket plus an ordered outbound queue drained by its own task, so
  emitting never blocks the caller and messages keep their order."""

  def __init__(self, socket: WebSocket, user_id: str):
    self.socket = socket
    self.user_id = user_id
    self._queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    self._task = asyncio.create_task(self._drain())

  def send(self, payload: str) -> None:
    self._queue.put_nowait(payload)

  async def _drain(self) -> None:
    while True:
      payload = await self._queue.get()
      if payload is None:
        return
      try:
        await self.socket.send_text(payload)
      except Exception:  # noqa: BLE001 -- the socket closed; drop the rest
        return

  async def close(self) -> None:
    self._queue.put_nowait(None)
    try:
      await asyncio.wait_for(self._task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
      self._task.cancel()


_rooms: dict[str, list[Connection]] = {}


def join(project_id: str, connection: Connection) -> None:
  _rooms.setdefault(project_id, []).append(connection)


def leave(project_id: str, connection: Connection) -> None:
  room = _rooms.get(project_id)
  if room and connection in room:
    room.remove(connection)
    if not room:
      del _rooms[project_id]


def broadcast(project_id: str, message: dict[str, Any]) -> None:
  payload = json.dumps(message)
  for connection in _rooms.get(project_id, []):
    connection.send(payload)


def send_to_user(project_id: str, user_id: str, message: dict[str, Any]) -> None:
  payload = json.dumps(message)
  for connection in _rooms.get(project_id, []):
    if connection.user_id == user_id:
      connection.send(payload)


SHARED_EVENT_TYPES = {"testcases.proposed", "testcases.updated", "usage.update"}


def chat_emit(project_id: str, user_id: str) -> Emit:
  """The emitter for one user's chat turn: asset events to the whole project,
  everything else only to that user."""

  def emit(event: dict[str, Any]) -> None:
    if event.get("type") in SHARED_EVENT_TYPES:
      broadcast(project_id, event)
    else:
      send_to_user(project_id, user_id, event)

  return emit


def project_emit(project_id: str) -> Emit:
  return lambda event: broadcast(project_id, event)
