from django.contrib import admin, messages
from django.db.models import Q
import re
from django.utils import timezone
from django.urls import path
from django.shortcuts import redirect

from .models import (
    ClickupConfiguration,
    ClickupTask,
    GithubConfiguration,
    GithubRepo,
    GithubBranch,
    GithubCommit,
    GithubCommitTimeLog,
)


@admin.register(ClickupConfiguration)
class ClickupConfigurationAdmin(admin.ModelAdmin):
    list_display = ("clickup_user_name", "clickup_user_email", "last_fetched_at")
    actions = ("fetch_clickup_tasks",)

    def has_add_permission(self, request):
        # Enforce singleton
        return ClickupConfiguration.objects.count() == 0

    def fetch_clickup_tasks(self, request, queryset):
        # Use default_team_id on the first (and only) config
        cfg = ClickupConfiguration.objects.first()
        if not cfg or not cfg.api_token:
            self.message_user(request, "ClickUp Configuration missing or token not set.", level=messages.ERROR)
            return
        if not cfg.default_team_id:
            self.message_user(request, "Set default_team_id on ClickUp Configuration to fetch tasks.", level=messages.ERROR)
            return
        try:
            from .services.clickup_client import ClickUpClient

            client = ClickUpClient(cfg.api_token)
            created = 0
            updated = 0
            seen_ids = set()
            for task in client.iter_team_tasks(cfg.default_team_id, include_closed=True):
                obj = ClickupTask.upsert_from_payload(task, team_id=cfg.default_team_id)
                if obj:
                    seen_ids.add(obj.clickup_id)
            # We won't inactivate/delete tasks automatically; ClickUp tasks are historical
            self.message_user(request, f"Fetched/updated {len(seen_ids)} ClickUp tasks for team {cfg.default_team_id}", level=messages.INFO)
        except Exception as e:
            self.message_user(request, f"Error fetching ClickUp tasks: {e}", level=messages.ERROR)

    fetch_clickup_tasks.short_description = "Fetch ClickUp Tasks (default team)"


@admin.register(GithubConfiguration)
class GithubConfigurationAdmin(admin.ModelAdmin):
    list_display = ("username", "user_id", "primary_email", "last_fetched_at")

    def has_add_permission(self, request):
        # Enforce singleton
        return GithubConfiguration.objects.count() == 0


@admin.register(GithubRepo)
class GithubRepoAdmin(admin.ModelAdmin):
    list_display = ("full_name", "is_active", "last_branches_synced_at")
    search_fields = ("name", "full_name", "owner_login")
    list_filter = ("is_active",)
    actions = ("fetch_branches_action",)

    def fetch_branches_action(self, request, queryset):
        from .services.github_client import GitHubClient
        cfg = GithubConfiguration.objects.first()
        if not cfg or not cfg.personal_access_token:
            self.message_user(request, "GitHub Configuration missing or token not set.", level=messages.ERROR)
            return
        gh = GitHubClient(cfg.personal_access_token, api_base_url=cfg.api_base_url)

        total_created = 0
        total_updated = 0
        total_inactivated = 0
        now = timezone.now()

        for repo in queryset:
            existing = {b.name: b for b in repo.branches.all()}
            seen = set()
            for b in gh.list_branches(repo.owner, repo.repo_name):
                name = b.get("name")
                if not name:
                    continue
                # Only include branches the configured user has worked on
                has_user_commit = False
                if cfg.username:
                    try:
                        has_user_commit = gh.branch_has_author_commit(repo.owner, repo.repo_name, name, cfg.username)
                    except Exception:
                        has_user_commit = False
                if not has_user_commit:
                    continue
                seen.add(name)
                commit_sha = (b.get("commit") or {}).get("sha")
                protected = bool(b.get("protected"))
                obj, created = GithubBranch.objects.update_or_create(
                    repo=repo,
                    name=name,
                    defaults={
                        "is_active": True,
                        "head_sha": commit_sha,
                        "protected": protected,
                    },
                )
                # Try to map branch to ClickUp task via tokens in branch name
                try:
                    tokens = re.findall(r"[A-Za-z0-9._-]+", name)
                    if tokens:
                        task = ClickupTask.objects.filter(Q(user_friendly_id__in=tokens) | Q(clickup_id__in=tokens)).first()
                        if task and obj.clickup_task_id != task.id:
                            obj.clickup_task = task
                            obj.save(update_fields=["clickup_task", "updated_at"])
                except Exception:
                    pass
                if created:
                    total_created += 1
                else:
                    total_updated += 1

            # Soft-remove branches not seen
            for name, branch in existing.items():
                if name not in seen and branch.is_active:
                    branch.is_active = False
                    branch.save(update_fields=["is_active", "updated_at"])
                    total_inactivated += 1

            repo.last_branches_synced_at = now
            repo.save(update_fields=["last_branches_synced_at", "updated_at"])

        self.message_user(
            request,
            f"Branches synced: created {total_created}, updated {total_updated}, inactivated {total_inactivated}",
            level=messages.INFO,
        )

    fetch_branches_action.short_description = "Fetch Branches"


@admin.register(GithubBranch)
class GithubBranchAdmin(admin.ModelAdmin):
    list_display = ("repo", "name", "is_active", "head_sha", "last_commits_synced_at")
    list_filter = ("is_active", "repo")
    search_fields = ("name", "repo__full_name")
    actions = ("sync_selected_branches",)

    def sync_selected_branches(self, request, queryset):
        from .services.github_client import GitHubClient
        cfg = GithubConfiguration.objects.first()
        if not cfg or not cfg.personal_access_token:
            self.message_user(request, "GitHub Configuration missing or token not set.", level=messages.ERROR)
            return
        gh = GitHubClient(cfg.personal_access_token, api_base_url=cfg.api_base_url)

        total_created = 0
        total_updated = 0

        for branch in queryset:
            owner = branch.repo.owner
            repo_name = branch.repo.repo_name
            since_iso = None
            if branch.last_commits_synced_at:
                since_iso = branch.last_commits_synced_at.isoformat()

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
                        # If it doesn't match preferred emails but author login matches username, still allow
                        if (gh_author or {}).get("login") != (cfg.username or ""):
                            continue

                message = commit_info.get("message") or ""
                author_name = author_info.get("name") or (gh_author.get("login") if gh_author else "")
                author_email = author_info.get("email") or ""
                date_str = author_info.get("date")
                try:
                    # Parse ISO8601; Django can parse on save if using fromisoformat? Safer to use timezone
                    from django.utils.dateparse import parse_datetime

                    date_dt = parse_datetime(date_str)
                except Exception:
                    date_dt = timezone.now()

                obj, created = GithubCommit.objects.get_or_create(
                    branch=branch, sha=sha,
                    defaults={
                        "message": message,
                        "author_name": author_name,
                        "author_email": author_email,
                        "date": date_dt or timezone.now(),
                        "html_url": item.get("html_url"),
                    },
                )
                if created:
                    total_created += 1
                    # Parse and store time log from the commit message
                    obj.refresh_time_log_from_message()
                else:
                    # Update message if changed
                    obj.update_message_if_changed(message)
                    total_updated += 1

            branch.last_commits_synced_at = timezone.now()
            branch.save(update_fields=["last_commits_synced_at", "updated_at"])

        self.message_user(
            request,
            f"Commits synced: created {total_created}, updated {total_updated}",
            level=messages.INFO,
        )

    sync_selected_branches.short_description = "Sync Branch"


@admin.register(GithubCommit)
class GithubCommitAdmin(admin.ModelAdmin):
    list_display = ("sha", "branch", "author_name", "author_email", "date")
    search_fields = ("sha", "message", "author_name", "author_email", "branch__name", "branch__repo__full_name")
    list_filter = ("branch__repo",)
    readonly_fields = ("created_at", "updated_at")
    actions = ("refresh_time_logs",)

    def refresh_time_logs(self, request, queryset):
        created = 0
        updated = 0
        deleted = 0
        unchanged = 0
        for commit in queryset:
            prev_log = getattr(commit, "time_log", None)
            prev_minutes = prev_log.total_minutes if prev_log else None
            commit.refresh_time_log_from_message()
            new_log = getattr(commit, "time_log", None)
            if prev_log is None and new_log is not None:
                created += 1
            elif prev_log is not None and new_log is None:
                deleted += 1
            elif prev_log is not None and new_log is not None:
                if new_log.total_minutes != prev_minutes:
                    updated += 1
                else:
                    unchanged += 1

        self.message_user(
            request,
            f"Time logs refreshed. created={created}, updated={updated}, deleted={deleted}, unchanged={unchanged}",
            level=messages.INFO,
        )

    refresh_time_logs.short_description = "Refresh time logs from commit messages"



@admin.register(GithubCommitTimeLog)
class GithubCommitTimeLogAdmin(admin.ModelAdmin):
    list_display = ("commit", "task_id", "clickup_task", "total_minutes", "parsed_from_message", "is_synced_with_clickup", "synced_at")
    search_fields = ("task_id", "commit__sha", "commit__message", "commit__branch__name", "commit__branch__repo__full_name", "clickup_task__user_friendly_id", "clickup_task__clickup_id")
    list_filter = ("is_synced_with_clickup",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(ClickupTask)
class ClickupTaskAdmin(admin.ModelAdmin):
    list_display = ("user_friendly_id", "clickup_id", "name", "status", "team_id", "list_id")
    search_fields = ("user_friendly_id", "clickup_id", "name", "status")
    actions = ("fetch_clickup_tasks",)
    change_list_template = "admin/tracker/clickuptask/change_list.html"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path("sync/", self.admin_site.admin_view(self.sync_tasks_view), name="tracker_clickuptask_sync"),
        ]
        return custom + urls

    def _sync_clickup_tasks(self, request):
        from .models import ClickupConfiguration
        cfg = ClickupConfiguration.objects.first()
        if not cfg or not cfg.api_token:
            self.message_user(request, "ClickUp Configuration missing or token not set.", level=messages.ERROR)
            return
        if not cfg.default_team_id:
            self.message_user(request, "Set default_team_id on ClickUp Configuration to fetch tasks.", level=messages.ERROR)
            return
        try:
            from .services.clickup_client import ClickUpClient

            client = ClickUpClient(cfg.api_token)
            seen_ids = set()
            for task in client.iter_team_tasks(cfg.default_team_id, include_closed=True):
                obj = ClickupTask.upsert_from_payload(task, team_id=cfg.default_team_id)
                if obj:
                    seen_ids.add(obj.clickup_id)
            self.message_user(request, f"Fetched/updated {len(seen_ids)} ClickUp tasks for team {cfg.default_team_id}", level=messages.INFO)
        except Exception as e:
            self.message_user(request, f"Error fetching ClickUp tasks: {e}", level=messages.ERROR)

    def sync_tasks_view(self, request):
        self._sync_clickup_tasks(request)
        return redirect("admin:tracker_clickuptask_changelist")

    def fetch_clickup_tasks(self, request, queryset):
        # Reuse the same logic as custom view
        self._sync_clickup_tasks(request)

    fetch_clickup_tasks.short_description = "Fetch ClickUp Tasks (default team)"

