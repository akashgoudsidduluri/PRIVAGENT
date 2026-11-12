"""GitHub repository inspection through authenticated REST endpoints."""
from __future__ import annotations
import re
from .base import NativeConnector, ConnectorError

_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class GitHubConnector(NativeConnector):
    origin = "https://api.github.com"
    service = "GitHub"

    def _repo(self, owner: str, repo: str) -> str:
        if not _NAME.fullmatch(owner) or not _NAME.fullmatch(repo):
            raise ConnectorError("invalid repository identifier")
        return f"/repos/{owner}/{repo}"

    def query_issues(self, owner: str, repo: str, state: str = "open") -> list[dict]:
        data = self._request("GET", self._repo(owner, repo) + "/issues", params={"state": state, "per_page": 50})
        return [item for item in data if "pull_request" not in item]

    def inspect_prs(self, owner: str, repo: str, state: str = "open") -> list[dict]:
        return self._request("GET", self._repo(owner, repo) + "/pulls", params={"state": state, "per_page": 50})

    def inspect_commit_diff(self, owner: str, repo: str, sha: str) -> dict:
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
            raise ConnectorError("invalid commit sha")
        return self._request("GET", self._repo(owner, repo) + f"/commits/{sha}")
