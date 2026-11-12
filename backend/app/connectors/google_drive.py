"""Drive search, text extraction, and small deliverable uploads."""
from __future__ import annotations
import json
import mimetypes
import secrets
from urllib.parse import quote
from .base import NativeConnector, ConnectorError


class GoogleDriveConnector(NativeConnector):
    origin = "https://www.googleapis.com"
    service = "Google Drive"

    def search_documents(self, query: str, limit: int = 50) -> list[dict]:
        safe = query.replace("'", "\\'")[:150]
        data = self._request("GET", "/drive/v3/files", params={"q": f"name contains '{safe}' and trashed = false",
                      "fields": "nextPageToken,files(id,name,mimeType,webViewLink)", "pageSize": min(max(limit, 1), 100)})
        return data.get("files", [])

    def extract_text(self, file_id: str, mime_type: str) -> str:
        path = f"/drive/v3/files/{quote(file_id, safe='')}"
        if mime_type == "application/vnd.google-apps.document":
            data = self._request("GET", path + "/export", params={"mimeType": "text/plain"})
        elif mime_type in ("text/plain", "text/csv", "text/markdown"):
            data = self._request("GET", path, params={"alt": "media"})
        else:
            raise ConnectorError("unsupported extraction format")
        return (data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data))[:200_000]

    def upload_deliverable(self, filename: str, data: bytes, mime_type: str | None = None) -> dict:
        if len(data) > 5 * 1024 * 1024 or not data:
            raise ConnectorError("upload must be between 1 byte and 5 MiB")
        media_type = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        boundary = "privagent-" + secrets.token_hex(16)
        metadata = json.dumps({"name": filename}).encode()
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + metadata +
                f"\r\n--{boundary}\r\nContent-Type: {media_type}\r\n\r\n".encode() + data + f"\r\n--{boundary}--\r\n".encode())
        return self._request("POST", "/upload/drive/v3/files", params={"uploadType": "multipart", "fields": "id,name,webViewLink"},
                             content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
