import re
from typing import Dict, Optional, Union

from django.db.models import Q
from django.utils import timezone

from .models import (
    GithubConfiguration,
    GithubRepo,
    GithubBranch,
    GithubCommit,
    ClickupTask,
)
from .services.github_client import GitHubClient

"""
Reusable sync and processing utilities for GitHub/ClickUp integration.

These functions are designed to be stateless and return plain dict summaries,
so they can be invoked as future async tasks (e.g., via Zappa) without needing
Django model instances on the caller side.
"""


def _get_github_client() -> GitHubClient:
    cfg = GithubConfiguration.objects.first()
    if not cfg or not cfg.personal_access_token:
        raise ValueError("GitHub configuration missing or token not set")
    return GitHubClient(cfg.personal_access_token, api_base_url=cfg.api_base_url)


def map_branch_clickup_task_by_name_tokens(branch_or_id: Union[int, GithubBranch]) -> Optional[int]:
    """Attempt to map a GithubBranch to a ClickupTask by scanning branch name tokens.

    Returns ClickupTask.id if mapping was set/exists, else None.
    """
    branch = GithubBranch.objects.get(pk=branch_or_id) if isinstance(branch_or_id, int) else branch_or_id
    tokens = re.findall(r"[A-Za-z0-9._-]+", branch.name or "")
    if not tokens:
        return None
    task = (
        ClickupTask.objects.filter(Q(user_friendly_id__in=tokens) | Q(clickup_id__in=tokens)).first()
    )
    if task and branch.clickup_task_id != task.id:
        branch.clickup_task = task
        branch.save(update_fields=["clickup_task", "updated_at"])
    return task.id if task else None


def fetch_branches(repo_or_id: Union[int, GithubRepo], only_user_branches: bool = True) -> Dict:
    """Fetch branches from GitHub for a repo and upsert GithubBranch rows.

    - If only_user_branches is True, include only branches with at least one commit by the configured user.
    - Also attempts to map branches to ClickUp tasks using name tokens.
    """
    repo = GithubRepo.objects.get(pk=repo_or_id) if isinstance(repo_or_id, int) else repo_or_id
    gh = _get_github_client()
    cfg = GithubConfiguration.objects.first()

    existing = {b.name: b for b in repo.branches.all()}
    seen: set[str] = set()
    created = 0
    updated = 0
    inactivated = 0

    for b in gh.list_branches(repo.owner, repo.repo_name):
        name = b.get("name")
        if not name:
            continue
        if only_user_branches and cfg and cfg.username:
            try:
                if not gh.branch_has_author_commit(repo.owner, repo.repo_name, name, cfg.username):
                    continue
            except Exception:
                continue
        seen.add(name)
        commit_sha = (b.get("commit") or {}).get("sha")
        protected = bool(b.get("protected"))
        obj, was_created = GithubBranch.objects.update_or_create(
            repo=repo,
            name=name,
            defaults={
                "is_active": True,
                "head_sha": commit_sha,
                "protected": protected,
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1
        # Attempt mapping to ClickUp task
        map_branch_clickup_task_by_name_tokens(obj)

    # Soft inactivate not-seen branches
    for name, branch in existing.items():
        if name not in seen and branch.is_active:
            branch.is_active = False
            branch.save(update_fields=["is_active", "updated_at"])
            inactivated += 1

    repo.last_branches_synced_at = timezone.now()
    repo.save(update_fields=["last_branches_synced_at", "updated_at"])

    return {
        "repo_id": repo.id,
        "created": created,
        "updated": updated,
        "inactivated": inactivated,
        "active_count": repo.branches.filter(is_active=True).count(),
    }


def process_commit_time_log(commit_or_id: Union[int, GithubCommit]) -> Dict:
    """Parse and upsert the time log for a single commit using smart commit rules.

    Returns a summary dict containing commit_id and whether a log exists after processing.
    """
    commit = GithubCommit.objects.get(pk=commit_or_id) if isinstance(commit_or_id, int) else commit_or_id
    before = bool(getattr(commit, "time_log", None))
    commit.refresh_time_log_from_message()
    commit.refresh_from_db()
    after = bool(getattr(commit, "time_log", None))
    return {"commit_id": commit.id, "had_log_before": before, "has_log_after": after}


def sync_branch_commits(branch_or_id: Union[int, GithubBranch], since_last: bool = True) -> Dict:
    """Sync commits for one branch from GitHub and process time logs.

    - since_last: if True, request only commits since last_commits_synced_at.
    - Filters by configured username and preferred_author_emails when available.
    """
    branch = GithubBranch.objects.get(pk=branch_or_id) if isinstance(branch_or_id, int) else branch_or_id
    cfg = GithubConfiguration.objects.first()
    if not cfg or not cfg.personal_access_token:
        raise ValueError("GitHub configuration missing or token not set")
    gh = GitHubClient(cfg.personal_access_token, api_base_url=cfg.api_base_url)

    owner = branch.repo.owner
    repo_name = branch.repo.repo_name
    since_iso = None
    if since_last and branch.last_commits_synced_at:
        since_iso = branch.last_commits_synced_at.isoformat()

    created = 0
    updated = 0
    processed = 0

    for item in gh.list_commits(owner, repo_name, sha=branch.name, author=cfg.username or None, since=since_iso):
        sha = item.get("sha")
        if not sha:
            continue
        commit_info = item.get("commit") or {}
        author_info = commit_info.get("author") or {}
        gh_author = item.get("author") or {}

        # Optional additional filtering by email if provided
        if cfg.preferred_author_emails:
            email = (author_info or {}).get("email")
            if email and email not in set(cfg.preferred_author_emails or []):
                if (gh_author or {}).get("login") != (cfg.username or ""):
                    continue

        message = commit_info.get("message") or ""
        author_name = author_info.get("name") or (gh_author.get("login") if gh_author else "")
        author_email = author_info.get("email") or ""
        date_str = author_info.get("date")
        try:
            from django.utils.dateparse import parse_datetime

            date_dt = parse_datetime(date_str)
        except Exception:
            date_dt = timezone.now()

        obj, was_created = GithubCommit.objects.get_or_create(
            branch=branch,
            sha=sha,
            defaults={
                "message": message,
                "author_name": author_name,
                "author_email": author_email,
                "date": date_dt or timezone.now(),
                "html_url": item.get("html_url"),
            },
        )
        if was_created:
            created += 1
            obj.refresh_time_log_from_message()
            processed += 1
        else:
            obj.update_message_if_changed(message)
            processed += 1

    branch.last_commits_synced_at = timezone.now()
    branch.save(update_fields=["last_commits_synced_at", "updated_at"])

    return {
        "branch_id": branch.id,
        "created": created,
        "updated": updated,
        "processed": processed,
    }


def sync_repo(repo_or_id: Union[int, GithubRepo], only_user_branches: bool = True) -> Dict:
    """Orchestrate a full sync for a repository: fetch branches and sync commits for each active branch."""
    repo = GithubRepo.objects.get(pk=repo_or_id) if isinstance(repo_or_id, int) else repo_or_id
    b_stats = fetch_branches(repo, only_user_branches=only_user_branches)

    total_created = 0
    total_updated = 0
    total_processed = 0

    for branch in repo.branches.filter(is_active=True).all():
        s = sync_branch_commits(branch)
        total_created += s.get("created", 0)
        total_updated += s.get("updated", 0)
        total_processed += s.get("processed", 0)

    return {
        "repo_id": repo.id,
        "branches": b_stats,
        "commits_created": total_created,
        "commits_updated": total_updated,
        "commits_processed": total_processed,
    }


def sync_all_repos(only_user_branches: bool = True) -> Dict:
    """Sync all active repositories for the configured user."""
    results = []
    for repo in GithubRepo.objects.filter(is_active=True).all():
        results.append(sync_repo(repo, only_user_branches=only_user_branches))
    return {"repos": results}
