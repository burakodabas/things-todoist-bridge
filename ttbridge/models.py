"""Shared data model used by both sides of the bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

# Fields that the sync engine compares and merges.
FIELDS = ("title", "notes", "when", "deadline", "tags", "completed")


def norm_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def norm_tags(tags) -> Tuple[str, ...]:
    if not tags:
        return ()
    if isinstance(tags, str):
        tags = [t for t in tags.split(",")]
    return tuple(sorted({t.strip() for t in tags if t and t.strip()}, key=str.casefold))


@dataclass
class Item:
    """A to-do as seen from either Things or Todoist, normalised."""

    id: str
    title: str
    notes: str = ""
    when: Optional[str] = None  # YYYY-MM-DD (Things "when" / Todoist due date)
    deadline: Optional[str] = None  # YYYY-MM-DD
    tags: Tuple[str, ...] = ()
    completed: bool = False
    modified: float = 0.0  # epoch seconds
    deleted: bool = False
    recurring: bool = False
    # Where the item lives, used for scope checks.
    project: Optional[str] = None  # Things project id / Todoist project id
    area: Optional[str] = None  # Things area id
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.title = norm_text(self.title)
        self.notes = norm_text(self.notes)
        self.tags = norm_tags(self.tags)
        self.when = self.when or None
        self.deadline = self.deadline or None
        self.completed = bool(self.completed)

    def fields(self, drop_tags: Tuple[str, ...] = ()) -> Dict[str, Any]:
        drop = {t.casefold() for t in drop_tags}
        return {
            "title": self.title,
            "notes": self.notes,
            "when": self.when,
            "deadline": self.deadline,
            "tags": [t for t in self.tags if t.casefold() not in drop],
            "completed": self.completed,
        }
