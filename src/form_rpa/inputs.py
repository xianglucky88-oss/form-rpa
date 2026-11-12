from __future__ import annotations
import json
from pathlib import Path


class FormSource:
    def read(self) -> list[dict]:
        raise NotImplementedError


class JsonFormSource(FormSource):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8"))
