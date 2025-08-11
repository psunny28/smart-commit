import json
import urllib.parse
import urllib.request
from typing import Dict, Optional

from django.conf import settings


class ClickUpError(Exception):
    pass


class ClickUpClient:
    def __init__(self, api_token: str, api_base_url: Optional[str] = None, user_agent: str = "github_clickup_logger/1.0") -> None:
        self.api_token = api_token
        self.api_base_url = (api_base_url or getattr(settings, "CLICKUP_API_BASE_URL", "https://api.clickup.com/api")).rstrip("/")
        self.user_agent = user_agent
        self.default_timeout = getattr(settings, "CLICKUP_HTTP_TIMEOUT", 15)

    def _request(self, method: str, path: str, params: Optional[Dict[str, str]] = None) -> Dict:
        url = f"{self.api_base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url=url, method=method)
        req.add_header("Authorization", self.api_token)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", self.user_agent)
        try:
            with urllib.request.urlopen(req, timeout=self.default_timeout) as resp:
                data = resp.read()
                return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8")
            except Exception:
                body = str(e)
            raise ClickUpError(f"ClickUp API error {e.code} for {path}: {body}") from e
        except urllib.error.URLError as e:
            raise ClickUpError(f"ClickUp API connection error for {path}: {e}") from e

    def get_user(self) -> Dict:
        # v2 user endpoint returns { user: {...} }
        return self._request("GET", "/v2/user")

    def list_team_tasks(self, team_id: str, page: int = 0, include_closed: bool = True, page_size: int = 100) -> Dict:
        """Return one page of tasks for a team. Use iter_team_tasks for an iterator.

        ClickUp supports GET /v2/team/{team_id}/task with filters.
        We'll expose a minimal subset and rely on pagination by page parameter.
        """
        params = {
            "page": str(page),
            "include_closed": str(include_closed).lower(),
            "subtasks": "true",
            "page_size": str(page_size),
        }
        return self._request("GET", f"/v2/team/{team_id}/task", params=params)

    def iter_team_tasks(self, team_id: str, include_closed: bool = True, page_size: int = 100, max_pages: int = 100):
        page = 0
        while page < max_pages:
            payload = self.list_team_tasks(team_id, page=page, include_closed=include_closed, page_size=page_size)
            tasks = (payload or {}).get("tasks") or []
            if not tasks:
                break
            for t in tasks:
                yield t
            page += 1
