"""WebSocket message schema (client -> server).

A client must send {"type": "hello", "last_seq": N} after connecting: the server answers with the events
after N and only then starts live delivery (events, state, policy, result broadcasts).

Server -> client messages are plain dicts:

  {"type": "event", "seq": n, "kind": "...", "ts": ..., ...}   timeline item (buffered, resumable)
  {"type": "state", "status": ..., "sandbox": ..., "preset": ..., "mode": "live"|"replay", ...}
  {"type": "policy", "yaml": "...", "preset": ..., "version": n}
  {"type": "result", "text": "...", "file": "..."}
  {"type": "sandboxes", "items": [...]}
  {"type": "reset"}                                             session was reset: clear the timeline, last_seq = 0
  {"type": "ack" | "error", "ref": "<client msg id>", "message": "..."}
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter


class _Base(BaseModel):
    id: str = ""


class Hello(_Base):
    type: Literal["hello"]
    last_seq: int = Field(default=0, ge=0)


class RunTask(_Base):
    type: Literal["run_task"]
    task: str = Field(min_length=1, max_length=2000)
    url: str = Field(default="", max_length=2000, pattern=r"^(https?://\S+)?$")


class Retry(_Base):
    type: Literal["retry"]


class Stop(_Base):
    type: Literal["stop"]


class ApplyPreset(_Base):
    type: Literal["apply_preset"]
    preset: Literal["strict", "research"]


class ApplyPolicy(_Base):
    type: Literal["apply_policy"]
    yaml: str = Field(min_length=1, max_length=200_000)


class AllowHost(_Base):
    type: Literal["allow_host"]
    host: str = Field(pattern=r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
    port: int = Field(default=443, ge=1, le=65535)
    binary: str = Field(default="", max_length=512)


class GetPolicy(_Base):
    type: Literal["get_policy"]


class ListSandboxes(_Base):
    type: Literal["list_sandboxes"]


class DeleteSandbox(_Base):
    type: Literal["delete_sandbox"]
    name: str = Field(default="", pattern=r"^[A-Za-z0-9-]{0,63}$")  # empty = current session sandbox


ClientMessage = Annotated[
    Union[Hello, RunTask, Retry, Stop, ApplyPreset, ApplyPolicy, AllowHost, GetPolicy, ListSandboxes, DeleteSandbox],
    Field(discriminator="type"),
]
client_message_adapter: TypeAdapter = TypeAdapter(ClientMessage)
