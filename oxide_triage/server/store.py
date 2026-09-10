"""Conversation and result persistence for the web app.

Results are complete, self-describing objects and can be large, so each is written once to its
own JSON file and referenced by id from the conversations that use it. Conversations are small
JSON documents; the whole store lives beside the cache so a container volume keeps it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from oxide_triage.schemas import TriageResult


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class Focus(BaseModel):
    result_id: str
    candidate: str  # formula or material id, as the user or the canvas named it


class Scope(BaseModel):
    """What the composer's scope strip sends with a message. Every field is optional; a set
    field is applied on top of the parsed request and surfaces as a deviation like any other."""

    profile: str | None = None
    families: list[str] | None = None
    top_k: int | None = None
    min_band_gap_ev: float | None = None
    max_energy_above_hull_ev_atom: float | None = None
    max_elements: int | None = None


class Step(BaseModel):
    """One visible tool call inside an assistant turn."""

    tool: str
    label: str
    status: str = "done"  # running | done | failed
    detail: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    ms: int | None = None


class Pending(BaseModel):
    """A tool call held back for confirmation (clarify-before-run)."""

    id: str
    tool: str
    args: dict[str, Any]
    questions: list[str]


class Turn(BaseModel):
    id: str
    role: str  # user | assistant
    text: str = ""
    created_at: str = Field(default_factory=now_iso)
    focus: Focus | None = None
    scope: Scope | None = None
    steps: list[Step] = Field(default_factory=list)
    result_id: str | None = None
    explain: str | None = None  # explain / compare markdown attached to this turn
    suggestions: list[str] = Field(default_factory=list)
    pending: Pending | None = None
    error: str | None = None


class Conversation(BaseModel):
    id: str
    title: str = ""
    profile: str = "default"
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    turns: list[Turn] = Field(default_factory=list)
    result_ids: list[str] = Field(default_factory=list)  # in order of creation
    model_messages: list[dict[str, Any]] = Field(default_factory=list)  # the model driver's history
    driver: str = "rules"  # rules | claude

    @property
    def latest_result_id(self) -> str | None:
        return self.result_ids[-1] if self.result_ids else None

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "profile": self.profile,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "n_turns": len(self.turns),
            "driver": self.driver,
        }


def _short_id(blob: str) -> str:
    return hashlib.sha256(blob.encode()).hexdigest()[:10]


class SessionStore:
    """Conversations and results on disk, with an in-memory cache of recently used results."""

    def __init__(self, root: Path, result_capacity: int = 20):
        self.root = Path(root)
        self.conv_dir = self.root / "conversations"
        self.result_dir = self.root / "results"
        self.conv_dir.mkdir(parents=True, exist_ok=True)
        self.result_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._results: dict[str, TriageResult] = {}
        self._result_order: list[str] = []
        self._capacity = result_capacity

    # ---- results --------------------------------------------------------------------

    def put_result(self, result: TriageResult) -> str:
        blob = result.model_dump_json()
        rid = _short_id(blob)
        with self._lock:
            (self.result_dir / f"{rid}.json").write_text(blob, encoding="utf-8")
            self._remember(rid, result)
        return rid

    def get_result(self, rid: str) -> TriageResult | None:
        with self._lock:
            if rid in self._results:
                return self._results[rid]
            path = self.result_dir / f"{rid}.json"
            if not path.is_file():
                return None
            result = TriageResult.model_validate_json(path.read_text(encoding="utf-8"))
            self._remember(rid, result)
            return result

    def _remember(self, rid: str, result: TriageResult) -> None:
        self._results[rid] = result
        if rid in self._result_order:
            self._result_order.remove(rid)
        self._result_order.append(rid)
        while len(self._result_order) > self._capacity:
            old = self._result_order.pop(0)
            self._results.pop(old, None)

    # ---- conversations --------------------------------------------------------------

    def new_conversation(self, profile: str = "default", driver: str = "rules") -> Conversation:
        cid = _short_id(f"{now_iso()}-{profile}-{threading.get_ident()}-{len(self.list_conversations())}")
        conv = Conversation(id=cid, profile=profile, driver=driver)
        self.save_conversation(conv)
        return conv

    def get_conversation(self, cid: str) -> Conversation | None:
        path = self.conv_dir / f"{cid}.json"
        if not path.is_file():
            return None
        return Conversation.model_validate_json(path.read_text(encoding="utf-8"))

    def save_conversation(self, conv: Conversation) -> None:
        conv.updated_at = now_iso()
        with self._lock:
            (self.conv_dir / f"{conv.id}.json").write_text(conv.model_dump_json(), encoding="utf-8")

    def delete_conversation(self, cid: str) -> bool:
        path = self.conv_dir / f"{cid}.json"
        if not path.is_file():
            return False
        path.unlink()
        return True

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        items = []
        for path in self.conv_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items.append(Conversation.model_validate(data).summary())
        items.sort(key=lambda c: c["updated_at"], reverse=True)
        return items[:limit]
