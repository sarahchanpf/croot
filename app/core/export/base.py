"""Pluggable export destinations.

In-app results are the product; export is optional and behind this interface so
a future Google Sheets / Gem / ATS destination drops in without touching the
search or ranking code. Each destination takes the ranked candidates + search
metadata and returns a small result describing what it produced.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ExportResult:
    kind: str                       # "csv" | "sheet" | "gem" | ...
    filename: str | None = None     # for download-style destinations
    content: bytes | None = None    # inline payload (e.g. CSV bytes)
    url: str | None = None          # for remote destinations (a created Sheet)
    detail: str = ""


class Destination(ABC):
    kind: str

    @abstractmethod
    def write(self, candidates: list[dict], meta: dict) -> ExportResult:
        """Produce the export. `meta` carries the search summary (role,
        location, criteria, pool size, etc.) for any 'about this search' tab."""
        raise NotImplementedError
