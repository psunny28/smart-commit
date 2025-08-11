import json
import time
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Optional

from django.conf import settings


class GitHubError(Exception):
    pass


class GitHubClient:
    def __init__(self, token: str, api_base_url: Optional[str] = None, user_agent: str = "github_clickup_logger/1.0") -> None:
        self.token = token
        self.api_base_url = (api_base_url or getattr(settings, "GITHUB_API_BASE_URL", "https://api.github.com")).rstrip("/")
        self.user_agent = user_agent
        self.default_timeout = getattr(settings, "GITHUB_HTTP_TIMEOUT", 15)

    def _request(self, method: str, path: str, params: Optional[Dict[str, str]] = None) -> Dict:
        url = f"{self.api_base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url=url, method=method)
        req.add_header("Authorization", f"token {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", self.user_agent)
        try:
            with urllib.request.urlopen(req, timeout=self.default_timeout) as resp:
                data = resp.read()
                return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Surface useful error body if present
            try:
                body = e.read().decode("utf-8")
            except Exception:
                body = str(e)
            raise GitHubError(f"GitHub API error {e.code} for {path}: {body}") from e
        except urllib.error.URLError as e:
            raise GitHubError(f"GitHub API connection error for {path}: {e}") from e

    def get_authenticated_user(self) -> Dict:
        return self._request("GET", "/user")

    def search_commits_by_author(self, username: str, per_page: int = 100, max_pages: int = 10) -> Iterable[Dict]:
        # Note: Search commits may have preview headers in REST v3; Accept header above generally works.
        page = 1
        while page <= max_pages:
            payload = self._request(
                "GET",
                "/search/commits",
                params={"q": f"author:{username}", "per_page": str(per_page), "page": str(page)},
            )
            items = payload.get("items", [])
            if not items:
                break
            for item in items:
                yield item
            page += 1
            time.sleep(0.1)  # small sleep to be polite

    def list_user_repos(self, affiliation: str = "owner,collaborator,organization_member", per_page: int = 100, max_pages: int = 50) -> Iterable[Dict]:
        page = 1
        while page <= max_pages:
            items = self._request(
                "GET",
                "/user/repos",
                params={"affiliation": affiliation, "per_page": str(per_page), "page": str(page)},
            )
            if not items:
                break
            for repo in items:
                yield repo
            page += 1
            time.sleep(0.05)

    def list_branches(self, owner: str, repo: str, per_page: int = 100, max_pages: int = 50) -> Iterable[Dict]:
        page = 1
        while page <= max_pages:
            branches = self._request(
                "GET",
                f"/repos/{owner}/{repo}/branches",
                params={"per_page": str(per_page), "page": str(page)},
            )
            if not branches:
                break
            for b in branches:
                yield b
            page += 1

    def list_commits(self, owner: str, repo: str, sha: str, author: Optional[str] = None, since: Optional[str] = None, per_page: int = 100, max_pages: int = 50) -> Iterable[Dict]:
        page = 1
        params: Dict[str, str] = {"sha": sha, "per_page": str(per_page)}
        if author:
            params["author"] = author
        if since:
            params["since"] = since
        while page <= max_pages:
            params["page"] = str(page)
            commits = self._request("GET", f"/repos/{owner}/{repo}/commits", params=params)
            if not commits:
                break
            for c in commits:
                yield c
            page += 1

    def repo_has_author_commit(self, owner: str, repo: str, author: str) -> bool:
        commits = list(self.list_commits(owner, repo, sha="", author=author, per_page=1, max_pages=1))
        return len(commits) > 0

    def branch_has_author_commit(self, owner: str, repo: str, branch: str, author: str) -> bool:
        """Return True if the given branch has at least one commit by author.

        Uses a single-page request with per_page=1 for efficiency.
        """
        for _ in self.list_commits(owner, repo, sha=branch, author=author, per_page=1, max_pages=1):
            return True
        return False
