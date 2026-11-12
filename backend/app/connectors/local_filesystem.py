"""Artifact-root-only local document adapter; paths cannot escape its root."""
from __future__ import annotations
from pathlib import Path
from .base import ConnectorError


class LocalFileSystemAdapter:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).resolve() if root else None

    @property
    def configured(self) -> bool:
        return self.root is not None and self.root.is_dir()

    def _path(self, filename: str) -> Path:
        if not self.configured or not filename or Path(filename).name != filename:
            raise ConnectorError("invalid artifact path")
        path = (self.root / filename).resolve()
        if path.parent != self.root or path.is_symlink():
            raise ConnectorError("artifact path escapes root")
        return path

    def search_documents(self, query: str, limit: int = 50) -> list[dict]:
        if not self.configured:
            raise ConnectorError("artifact root is not configured")
        return [{"name": p.name, "size": p.stat().st_size} for p in self.root.iterdir()
                if p.is_file() and not p.is_symlink() and query.casefold() in p.name.casefold()][:limit]

    def extract_text(self, file_id: str, mime_type: str = "text/plain") -> str:
        if mime_type not in ("text/plain", "text/markdown", "text/csv"):
            raise ConnectorError("unsupported local extraction format")
        return self._path(file_id).read_text(encoding="utf-8")[:200_000]

    def upload_deliverable(self, filename: str, data: bytes, mime_type: str | None = None) -> dict:
        if not data or len(data) > 5 * 1024 * 1024:
            raise ConnectorError("invalid artifact size")
        path = self._path(filename)
        if path.exists():
            raise ConnectorError("artifact already exists")
        path.write_bytes(data)
        return {"name": filename, "size": len(data)}
