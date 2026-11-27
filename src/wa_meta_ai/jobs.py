"""Background job queue and optional webhook dispatcher for async generation."""

from __future__ import annotations

import asyncio
import json
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


@dataclass
class Job:
    job_id: str
    kind: str
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


def _post_webhook(url: str, payload: dict[str, Any]) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "wa-meta-ai/0.1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15):
        pass


class JobManager:
    """In-memory async job registry with optional webhook notifications."""

    def __init__(self, max_jobs: int = 200) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._max_jobs = max_jobs

    def submit(
        self,
        kind: str,
        coro_factory: Callable[[], Awaitable[dict[str, Any]]],
        webhook_url: str | None = None,
    ) -> Job:
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        job = Job(job_id=job_id, kind=kind)
        self._jobs[job_id] = job
        if len(self._jobs) > self._max_jobs:
            oldest = next(iter(self._jobs))
            self._jobs.pop(oldest, None)

        async def runner() -> None:
            job.status = "running"
            try:
                job.result = await coro_factory()
                job.status = "completed"
            except Exception as exc:
                job.error = str(exc)
                job.status = "failed"
            finally:
                job.finished_at = datetime.now(timezone.utc).isoformat()
                if webhook_url:
                    try:
                        await asyncio.to_thread(_post_webhook, webhook_url, job.to_dict())
                    except Exception:
                        pass

        task = asyncio.create_task(runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [j.to_dict() for j in list(self._jobs.values())[-limit:]][::-1]
