"""Frozen resource capacity rules shared by the merge, builder, and patch paths.

Catalog rows carry a byte budget fixed by the original ISO extent. These helpers
answer three questions about that budget: how many bytes a row may use, what text
fits after reviewed abbreviations, and whether a rebuilt native resource still
matches the original outside the fields it is allowed to change.
"""
from __future__ import annotations

import json
from typing import Any

from fls.core.env import SOURCE
from fls.core.path_compat import REPO


class ResourceCapacityError(ValueError):
    """A changed native resource cannot fit its frozen ISO extent."""

    def __init__(self, overflows: list[dict[str, Any]]) -> None:
        self.capacity_overflows = list(overflows)
        details = []
        for item in self.capacity_overflows:
            identifiers = item.get('translation_ids') or []
            id_text = f"; IDs: {', '.join(identifiers[:4])}" if identifiers else ''
            if len(identifiers) > 4:
                id_text += f" (+{len(identifiers)-4} more)"
            details.append(
                f"{item['file']} needs {item['required_bytes']} bytes "
                f"({item['required_sectors']} sectors), but its ISO extent has "
                f"{item['available_bytes']} bytes ({item['available_sectors']} sectors); "
                f"overflow {item['overflow_bytes']} bytes{id_text}"
            )
        super().__init__('ISO resource capacity exceeded: ' + '; '.join(details))


def capacity_overflows(event_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize event files whose rebuilt payload exceeds source allocation."""
    return [
        {
            'file': row['file'],
            'required_bytes': row['patched_bytes'],
            'available_bytes': row['allocated_bytes'],
            'overflow_bytes': row['patched_bytes'] - row['allocated_bytes'],
            'required_sectors': (row['patched_bytes'] + 2047) // 2048,
            'available_sectors': row['allocated_bytes'] // 2048,
            'translation_ids': row.get('translation_ids', []),
        }
        for row in event_reports
        if row['patched_bytes'] > row['allocated_bytes']
    ]


def catalog_capacity(row: dict[str, Any]) -> int:
    """Keep legacy input metadata, but never write ITEM's price at +18."""
    capacity = int(row['capacity_bytes'])
    if row.get('file') == 'SYSTEM/ITEM.DAT':
        assert int(row['offset'], 16) % 36 == 0, row['id']
        capacity = min(capacity, 18)
    return capacity


def catalog_text(row: dict[str, Any], text: str, language: str) -> str:
    """Apply reviewed capacity abbreviations without changing frozen drafts."""
    if row.get('file') != 'SYSTEM/ITEM.DAT' or language != 'en':
        return text
    recipe_path = REPO / 'config/patches/item-prices.json'
    if not recipe_path.is_file():
        return text
    recipe = json.loads(recipe_path.read_text(encoding='utf-8'))
    for field in recipe.get('fields', []):
        if int(row['offset'], 16) == int(field['offset'], 16):
            if text == field.get('before', {}).get('en-US'):
                return field.get('text', {}).get('en-US', text)
    return text


def verify_item_attributes(data: bytes | bytearray) -> None:
    """Verify that item attribute bytes (+18..+36) are preserved."""
    original = (SOURCE / 'SYSTEM/ITEM.DAT').read_bytes()
    assert len(data) == len(original) == 36 * 200
    for off in range(0, len(original), 36):
        assert data[off + 18:off + 36] == original[off + 18:off + 36], (
            'Item attributes changed',
            off // 36,
        )
