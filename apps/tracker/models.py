from django.db import models
from typing import Optional
import re

from django.conf import settings
from django.utils import timezone
import re


class ClickupConfiguration(models.Model):
    api_token = models.CharField(max_length=255)
    clickup_user_id = models.CharField(max_length=100, blank=True, null=True)
    clickup_user_name = models.CharField(max_length=255, blank=True, null=True)
    clickup_user_email = models.EmailField(blank=True, null=True)
    teams = models.JSONField(blank=True, null=True)
    default_team_id = models.CharField(max_length=100, blank=True, null=True)
    last_fetched_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return "ClickUp Configuration"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Deferred import to avoid circulars
        try:
            from .services.clickup_client import ClickUpClient, ClickUpError

            client = ClickUpClient(self.api_token)
            user_payload = client.get_user()
            user = user_payload.get("user") or user_payload
            if user:
                self.clickup_user_id = str(user.get("id") or "")
                self.clickup_user_name = user.get("username") or user.get("name")
                self.clickup_user_email = user.get("email")
            self.teams = user_payload.get("teams") or self.teams
            self.last_fetched_at = timezone.now()
            super().save(update_fields=[
                "clickup_user_id",
                "clickup_user_name",
                "clickup_user_email",
                "teams",
                "last_fetched_at",
                "updated_at",
            ])
        except Exception:
            # Swallow errors to not block admin save; surface via admin messages instead
            pass


class ClickupTask(models.Model):
    clickup_id = models.CharField(max_length=100, unique=True)
    user_friendly_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    name = models.CharField(max_length=500)
    status = models.CharField(max_length=100, blank=True, null=True)
    url = models.URLField(blank=True, null=True)
    team_id = models.CharField(max_length=100, blank=True, null=True)
    list_id = models.CharField(max_length=100, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.user_friendly_id or self.clickup_id}: {self.name}"

    @staticmethod
    def _derive_user_friendly_id(custom_id: Optional[str], name: Optional[str]) -> Optional[str]:
        """Return a friendly ID only if it matches the expected pattern like 'I-002'.
        Rules:
        - If custom_id exists and matches pattern ^[A-Za-z]-\d{3}$, use it.
        - Else, attempt to extract from the very start of name with same pattern.
        - Otherwise return None.
        """
        pattern = re.compile(r"^[A-Za-z]-\d{3}$")
        if custom_id is not None:
            cid = str(custom_id).strip()
            if pattern.match(cid):
                return cid
        # Try from name prefix
        if name:
            m = re.match(r"\s*([A-Za-z]-\d{3})\b", name)
            if m:
                return m.group(1)
        return None

    @classmethod
    def upsert_from_payload(cls, payload: dict, team_id: Optional[str] = None):
        if not payload:
            return None
        task_id = str(payload.get("id") or "")
        if not task_id:
            return None
        defaults = {
            "name": payload.get("name") or "",
            "status": ((payload.get("status") or {}).get("status")) if isinstance(payload.get("status"), dict) else payload.get("status"),
            "url": payload.get("url") or payload.get("url") or None,
            "team_id": team_id,
            "list_id": str((payload.get("list") or {}).get("id") or payload.get("list_id") or "") or None,
        }
        # Derive constrained user-friendly id
        derived = cls._derive_user_friendly_id(payload.get("custom_id"), defaults["name"])
        defaults["user_friendly_id"] = derived
        obj, _ = cls.objects.update_or_create(
            clickup_id=task_id,
            defaults=defaults,
        )
        return obj


class GithubConfiguration(models.Model):
    personal_access_token = models.CharField(max_length=255)
    api_base_url = models.CharField(max_length=255, default="https://api.github.com")
    username = models.CharField(max_length=255, blank=True, null=True)
    user_id = models.BigIntegerField(blank=True, null=True)
    primary_email = models.EmailField(blank=True, null=True)
    preferred_author_emails = models.JSONField(blank=True, null=True)
    last_fetched_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return "GitHub Configuration"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Fetch authenticated user and populate basic fields
        try:
            from .services.github_client import GitHubClient, GitHubError

            gh = GitHubClient(self.personal_access_token, api_base_url=self.api_base_url)
            me = gh.get_authenticated_user()
            self.username = me.get("login")
            self.user_id = me.get("id")
            # Try to populate email if provided
            if not self.primary_email:
                self.primary_email = me.get("email")
            self.last_fetched_at = timezone.now()
            super().save(update_fields=["username", "user_id", "primary_email", "last_fetched_at", "updated_at"])

            # Populate repos where the user has commits using search API when possible
            repos_seen = set()
            if self.username:
                for item in gh.search_commits_by_author(self.username):
                    repo = item.get("repository") or {}
                    if not repo:
                        continue
                    repos_seen.add(repo.get("full_name"))
                    GithubRepo.upsert_from_payload(repo)
        except Exception:
            # Avoid raising in admin save; admins can use explicit actions later
            pass


class GithubRepo(models.Model):
    github_id = models.BigIntegerField(unique=True)
    name = models.CharField(max_length=255)
    full_name = models.CharField(max_length=255)
    html_url = models.URLField()
    default_branch = models.CharField(max_length=255, blank=True, null=True)
    private = models.BooleanField(default=False)
    owner_login = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    last_branches_synced_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.full_name

    @property
    def owner(self) -> str:
        return (self.full_name or ":").split("/")[0]

    @property
    def repo_name(self) -> str:
        parts = (self.full_name or "/").split("/")
        return parts[1] if len(parts) > 1 else self.name

    @classmethod
    def upsert_from_payload(cls, repo: dict):
        if not repo:
            return None
        obj, _created = cls.objects.update_or_create(
            github_id=repo.get("id"),
            defaults={
                "name": repo.get("name") or "",
                "full_name": repo.get("full_name") or "",
                "html_url": repo.get("html_url") or "",
                "default_branch": repo.get("default_branch") or None,
                "private": bool(repo.get("private")),
                "owner_login": (repo.get("owner") or {}).get("login") or "",
                "is_active": True,
            },
        )
        return obj


class GithubBranch(models.Model):
    repo = models.ForeignKey(GithubRepo, on_delete=models.CASCADE, related_name="branches")
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    head_sha = models.CharField(max_length=64, blank=True, null=True)
    protected = models.BooleanField(default=False)
    clickup_task = models.ForeignKey('ClickupTask', on_delete=models.SET_NULL, blank=True, null=True, related_name='branches')
    last_commits_synced_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["repo", "name"], name="uniq_repo_branch_name"),
        ]

    def __str__(self) -> str:
        return f"{self.repo.full_name}:{self.name}"


class GithubCommit(models.Model):
    branch = models.ForeignKey(GithubBranch, on_delete=models.CASCADE, related_name="commits")
    sha = models.CharField(max_length=64, db_index=True)
    message = models.TextField()
    author_name = models.CharField(max_length=255)
    author_email = models.EmailField()
    date = models.DateTimeField()
    html_url = models.URLField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["branch", "sha"], name="uniq_branch_sha"),
        ]

    def __str__(self) -> str:
        return f"{self.sha} ({self.branch})"

    def update_message_if_changed(self, new_message: str):
        if new_message is not None and new_message != self.message:
            self.message = new_message
            self.save(update_fields=["message", "updated_at"])
            # Refresh time log based on new message
            self.refresh_time_log_from_message()

    def refresh_time_log_from_message(self):
        """Parse the commit message for the FIRST smart commit token and upsert GithubCommitTimeLog.

        Supported formats (first occurrence wins):
        - Case 2 (task + time): "#<task_id> <time>" where <time> is 1h, 30m, or 1h30m (no spaces inside)
        - Case 1 (time only): "~<time>"
        Trailing punctuation is allowed just after the time token.
        If no token found, delete any existing time log.
        """
        message = self.message or ""

        pattern = re.compile(
            r"(#(?P<task_id>[A-Za-z0-9._-]+)\s+(?P<time_task>\d+h(?:\d+m)?|\d+m)|~(?P<time_only>\d+h(?:\d+m)?|\d+m))(?=\s|$|[\.,;:!\?\)\]])",
            re.IGNORECASE,
        )

        m = pattern.search(message)
        # Locate existing log instance (if any)
        try:
            log = getattr(self, "time_log", None)
        except Exception:
            log = None

        if not m:
            if log:
                log.delete()
            return

        # Determine kind and extract time string and optional task_id
        if m.group("time_task"):
            time_token = m.group("time_task")
            task_id = m.group("task_id")
            parsed_repr = f"#{task_id} {time_token}"
        else:
            time_token = m.group("time_only")
            task_id = None
            parsed_repr = f"~{time_token}"

        # Convert time token to minutes
        total_minutes = 0
        h = re.search(r"(?i)(\d+)h", time_token)
        mm = re.search(r"(?i)(\d+)m", time_token)
        if h:
            try:
                total_minutes += int(h.group(1)) * 60
            except Exception:
                pass
        if mm:
            try:
                total_minutes += int(mm.group(1))
            except Exception:
                pass

        if total_minutes <= 0:
            if log:
                log.delete()
            return

        # Try to resolve ClickUp task from task_id or branch mapping
        resolved_task = None
        if task_id:
            try:
                resolved_task = ClickupTask.objects.filter(models.Q(user_friendly_id=task_id) | models.Q(clickup_id=task_id)).first()
            except Exception:
                resolved_task = None
        if not resolved_task:
            try:
                resolved_task = getattr(self.branch, 'clickup_task', None)
            except Exception:
                resolved_task = None

        if log:
            # Mark unsynced on change
            log.total_minutes = total_minutes
            log.parsed_from_message = parsed_repr
            log.task_id = task_id
            log.clickup_task = resolved_task
            log.is_synced_with_clickup = False
            log.save(update_fields=[
                "total_minutes",
                "parsed_from_message",
                "task_id",
                "clickup_task",
                "is_synced_with_clickup",
                "updated_at",
            ])
        else:
            GithubCommitTimeLog.objects.create(
                commit=self,
                total_minutes=total_minutes,
                parsed_from_message=parsed_repr,
                task_id=task_id,
                clickup_task=resolved_task,
                is_synced_with_clickup=False,
            )


class GithubCommitTimeLog(models.Model):
    commit = models.OneToOneField(GithubCommit, on_delete=models.CASCADE, related_name="time_log")
    task_id = models.CharField(max_length=100, blank=True, null=True)
    clickup_task = models.ForeignKey(ClickupTask, on_delete=models.SET_NULL, blank=True, null=True, related_name="time_logs")
    total_minutes = models.PositiveIntegerField()
    parsed_from_message = models.CharField(max_length=255, blank=True, null=True)
    is_synced_with_clickup = models.BooleanField(default=False)
    synced_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.total_minutes}m for {self.commit.sha}"
