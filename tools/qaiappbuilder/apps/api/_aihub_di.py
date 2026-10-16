# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the AI Hub Device bounded context.

Provides job management and configuration services for AI Hub device
compilation and profiling workflows.

The services include:
- Job registry (in-memory, thread-safe)
- Configuration management (API token storage)
- Hub CLI integration
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container


__all__ = ["AiHubSite", "AiHubSiteContext", "AiHubServices", "build_aihub_services"]


# Maximum number of completed/errored jobs to retain in memory.
# Running jobs are never evicted. Once this limit is reached, the oldest
# terminal (done/error) jobs are dropped to prevent unbounded growth.
_MAX_TERMINAL_JOBS = 100


@dataclass
class AiHubJobRegistry:
    """Thread-safe in-memory registry for AI Hub jobs.

    Maps job_id → {"status": "running"|"done"|"error", "output": [lines], ...}

    Eviction policy: at most ``_MAX_TERMINAL_JOBS`` completed/errored jobs are
    kept. Running jobs are never evicted. This prevents unbounded memory growth
    when many jobs are submitted over the lifetime of the process.
    """

    _jobs: dict[str, dict] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _evict_terminal_jobs(self) -> None:
        """Drop oldest terminal (done/error) jobs when over the limit.

        Must be called with ``_lock`` already held.
        """
        terminal = [
            jid for jid, j in self._jobs.items()
            if j.get("status") in ("done", "error")
        ]
        if len(terminal) > _MAX_TERMINAL_JOBS:
            # Drop the oldest entries (dict preserves insertion order in Python 3.7+)
            to_drop = len(terminal) - _MAX_TERMINAL_JOBS
            for jid in terminal[:to_drop]:
                del self._jobs[jid]

    def prune(self) -> None:
        """Drop excess terminal jobs after a worker changes state."""
        with self._lock:
            self._evict_terminal_jobs()


    def get(self, job_id: str) -> dict | None:
        """Get a snapshot of a job by ID."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {**job, "output": list(job.get("output", []))}

    def create(self, job_id: str, initial_state: dict | None = None) -> None:
        """Create a job while reserving bounded space for its terminal state."""
        with self._lock:
            terminal = [
                jid
                for jid, job in self._jobs.items()
                if job.get("status") in ("done", "error")
            ]
            # A running job is never evicted. Reserve one terminal slot now so
            # this job can finish without temporarily exceeding the bound.
            if len(terminal) >= _MAX_TERMINAL_JOBS:
                del self._jobs[terminal[0]]
            self._jobs[job_id] = initial_state or {
                "status": "running",
                "output": [],
            }

    def update(self, job_id: str, updates: dict) -> None:
        """Update job state. Triggers eviction when a job transitions to terminal."""
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(updates)
                # Evict after a job finishes to keep memory bounded.
                if updates.get("status") in ("done", "error"):
                    self._evict_terminal_jobs()

    def append_output(self, job_id: str, line: str) -> None:
        """Append a line to job output."""
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["output"].append(line)


class AiHubSite(StrEnum):
    GLOBAL = "global"
    CHINA = "china"


@dataclass(frozen=True, slots=True)
class AiHubSiteMetadata:
    api_url: str
    web_url: str
    public_url: str
    config_filename: str
    environment_variable: str

SITE_METADATA = {
    AiHubSite.GLOBAL: AiHubSiteMetadata("https://workbench.aihub.qualcomm.com", "https://workbench.aihub.qualcomm.com", "https://aihub.qualcomm.com", "aihub_config.json", "AIHUB_API_TOKEN"),
    AiHubSite.CHINA: AiHubSiteMetadata("https://workbench.aihub.qualcomm.cn", "https://workbench.aihub.qualcomm.cn", "https://aihub.qualcomm.cn", "aihub_config_china.json", "AIHUB_CHINA_API_TOKEN"),
}


@dataclass
class AiHubConfigManager:
    """Manage one site's token under the application data root."""
    data_root: Path
    site: AiHubSite = AiHubSite.GLOBAL

    @property
    def metadata(self) -> AiHubSiteMetadata:
        return SITE_METADATA[self.site]

    def _config_dir(self) -> Path:
        path = self.data_root / "config"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _get_config_path(self) -> Path:
        return self._config_dir() / self.metadata.config_filename

    def load(self) -> dict:
        path = self._get_config_path()
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                pass
        return {}

    def _atomic_write(self, path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, config: dict) -> None:
        self._atomic_write(self._get_config_path(), json.dumps(config, ensure_ascii=False, indent=2))

    def get_api_token(self) -> str | None:
        return os.environ.get(self.metadata.environment_variable) or self.load().get("api_token")



@dataclass(frozen=True, slots=True)
class AiHubSiteContext:
    metadata: AiHubSiteMetadata
    job_registry: AiHubJobRegistry
    config_manager: AiHubConfigManager


@dataclass(frozen=True, slots=True)
class AiHubServices:
    sites: dict[AiHubSite, AiHubSiteContext]

    def for_site(self, site: AiHubSite | str = AiHubSite.GLOBAL) -> AiHubSiteContext:
        return self.sites[AiHubSite(site)]

    @property
    def job_registry(self) -> AiHubJobRegistry:
        return self.for_site().job_registry

    @property
    def config_manager(self) -> AiHubConfigManager:
        return self.for_site().config_manager


def build_aihub_services(container: "Container") -> AiHubServices:
    return AiHubServices(sites={site: AiHubSiteContext(metadata, AiHubJobRegistry(), AiHubConfigManager(container.data_paths.root, site)) for site, metadata in SITE_METADATA.items()})