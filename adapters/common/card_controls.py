"""Plan card controls without SDK objects, I/O, or changes to runner-issued tokens."""
from __future__ import annotations

from typing import NamedTuple

from .spec import STAMP_ROW_ACTION

MENU_LABEL = "操作を選ぶ…"
STAMP_PICKER_LABEL = "別の投稿"


class CardControls(NamedTuple):
    """Borrowed action records in source order; the adapter owns native rendering."""

    operations: tuple[dict, ...]
    links: tuple[dict, ...]
    stamps: tuple[dict, ...]

    @property
    def stamp_picker(self) -> dict | None:
        """The form can leave the menu only beside a complete five-stamp row."""
        return next((b for b in self.operations if b.get("id") == "stamp"), None) \
            if len(self.stamps) == 5 else None

    @property
    def menu(self) -> tuple[dict, ...]:
        """Operations not already available beside the complete stamp row."""
        picker = self.stamp_picker
        return tuple(b for b in self.operations if b is not picker)


def plan_controls(parts: dict) -> CardControls:
    """Partition validated display parts, keeping every action and its original record."""
    operations, links, stamps = [], [], []
    for row in parts.get("action_rows") or []:
        for button in row:
            if button.get("ui") == "link":
                links.append(button)
            elif button.get("id") == STAMP_ROW_ACTION:
                stamps.append(button)
            else:
                operations.append(button)
    return CardControls(tuple(operations), tuple(links), tuple(stamps))


def stamp_icon(label: str) -> str:
    """Use the canonical stamp emoji, retaining unknown labels from older runners."""
    from stamp_contract import STAMP_EMOJI
    return next((e for e in STAMP_EMOJI.values() if label.startswith(e)), label)


def stamp_label(button: dict) -> str:
    """A compact stamp label with a visible check mark for the account's own stamp."""
    return stamp_icon(button["label"]) + (" ✓" if button.get("style") == "primary" else "")
