from django.core.management.base import BaseCommand
from django.db import transaction

from apps.tracker.models import GithubCommit


class Command(BaseCommand):
    help = (
        "Parse commit messages to create/update GithubCommitTimeLog for each commit. "
        "Optionally filter by repo and/or branch."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--repo",
            dest="repo",
            help="Filter by repository full name (owner/name)",
        )
        parser.add_argument(
            "--branch",
            dest="branch",
            help="Filter by branch name",
        )
        parser.add_argument(
            "--only-missing",
            action="store_true",
            default=False,
            help="Only process commits missing a time log entry",
        )

    def handle(self, *args, **options):
        repo = options.get("repo")
        branch = options.get("branch")
        only_missing = options.get("only_missing")

        qs = GithubCommit.objects.select_related("branch", "branch__repo")
        if repo:
            qs = qs.filter(branch__repo__full_name=repo)
        if branch:
            qs = qs.filter(branch__name=branch)
        if only_missing:
            qs = qs.filter(time_log__isnull=True)

        total = qs.count()
        created = 0
        updated = 0
        deleted = 0
        unchanged = 0

        self.stdout.write(self.style.NOTICE(f"Processing {total} commits..."))

        with transaction.atomic():
            for commit in qs.iterator(chunk_size=500):
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

        self.stdout.write(
            self.style.SUCCESS(
                "Done. created={0}, updated={1}, deleted={2}, unchanged={3}".format(
                    created, updated, deleted, unchanged
                )
            )
        )
