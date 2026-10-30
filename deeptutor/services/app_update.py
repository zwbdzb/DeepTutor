"""Version checks and durable, launcher-managed PyPI updates."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import re
import subprocess  # nosec B404 - every launched argv is generated internally
import sys
import time
from typing import Any, Callable, Literal, Sequence
from urllib.parse import unquote, urljoin, urlsplit
import uuid

import httpx

from deeptutor.__version__ import __version__
from deeptutor.runtime.home import get_runtime_home
from deeptutor.runtime.process import is_process_alive
from deeptutor.services.file_io import atomic_write_json

GITHUB_LATEST_RELEASE_URL = "https://api.github.com/repos/HKUDS/DeepTutor/releases/latest"
GITHUB_LATEST_RELEASE_WEB_URL = "https://github.com/HKUDS/DeepTutor/releases/latest"
VERSION_CHECK_TTL_SECONDS = 24 * 60 * 60

# ThinkBuddy desktop release source. The desktop shell owns download/install
# (see desktop-shell/desktop/updater.py); the backend only mirrors its manifest
# so the web sidebar badge can light up without the browser crossing CORS.
THINKBUDDY_UPDATE_MANIFEST_URL = "https://thinkbuddy.hanyoai.com/update/latest.json"
# Set by the desktop shell when it spawns `deeptutor start` (see
# desktop-shell/desktop/process.py). It marks the desktop install shape and
# carries the shell's own four-segment version for comparison.
DESKTOP_SHELL_MANAGED_ENV = "DEEPTUTOR_SHELL_MANAGED"
DESKTOP_VERSION_ENV = "DEEPTUTOR_DESKTOP_VERSION"
DESKTOP_VERSION_CHECK_TTL_SECONDS = 60 * 60
LAUNCHER_PID_ENV = "DEEPTUTOR_LAUNCHER_PID"
SYSTEMD_UPDATE_REASON = (
    "In-app updates are unavailable under a systemd service because systemd may stop "
    "the update worker with the service. Stop the service, upgrade DeepTutor with "
    "the same Python environment, then start the service with systemctl."
)

InstallMode = Literal["pypi", "source", "docker", "unknown"]
JobStatus = Literal["pending", "handoff", "running", "restarting", "succeeded", "failed"]

_STABLE_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_CURRENT_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


class VersionCheckError(RuntimeError):
    """Raised when official release metadata cannot be validated."""


class UpdateRequestError(RuntimeError):
    """Raised when a managed update cannot be scheduled safely."""


class UpdateInProgressError(UpdateRequestError):
    """Raised when another update already owns the durable active slot."""


@dataclass(frozen=True, slots=True)
class Installation:
    mode: InstallMode
    current_version: str
    automatic_update: bool
    command: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    version: str
    name: str
    published_at: str
    url: str
    excerpt: str
    migration_warning: bool


@dataclass(frozen=True, slots=True)
class VersionCheckResult:
    current_version: str
    release: ReleaseInfo
    checked_at: str
    cached: bool

    @property
    def update_available(self) -> bool:
        return _version_tuple(self.release.version) > _version_tuple(self.current_version)


@dataclass(frozen=True, slots=True)
class DesktopReleaseInfo:
    version: str
    url: str
    published_at: str


@dataclass(frozen=True, slots=True)
class DesktopUpdateCheckResult:
    enabled: bool
    current_version: str
    version: str
    url: str
    published_at: str
    checked_at: str
    cached: bool
    error: str

    @property
    def update_available(self) -> bool:
        return (
            bool(self.enabled)
            and bool(self.version)
            and bool(self.current_version)
            and _desktop_is_newer(self.version, self.current_version)
        )


class DesktopUpdateError(RuntimeError):
    """Raised when the ThinkBuddy desktop manifest cannot be validated."""


@dataclass(frozen=True, slots=True)
class UpdateJob:
    id: str
    status: JobStatus
    current_version: str
    target_version: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    restart_home: str | None = None
    restart_argv: tuple[str, ...] = ()
    restart_count: int = 0
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> UpdateJob:
        if payload.get("schema_version") != 1:
            raise ValueError("Unsupported update job")
        status = str(payload.get("status") or "")
        if status not in {"pending", "handoff", "running", "restarting", "succeeded", "failed"}:
            raise ValueError("Invalid update status")
        restart_home = _optional_string(payload.get("restart_home"))
        restart_argv = _validate_restart_argv(payload.get("restart_argv"), home=restart_home)
        return cls(
            id=str(payload["id"]),
            status=status,  # type: ignore[arg-type]
            current_version=_normalise_stable_version(str(payload["current_version"])),
            target_version=_normalise_stable_version(str(payload["target_version"])),
            created_at=str(payload["created_at"]),
            started_at=_optional_string(payload.get("started_at")),
            finished_at=_optional_string(payload.get("finished_at")),
            error=_optional_string(payload.get("error")),
            restart_home=restart_home,
            restart_argv=restart_argv,
            restart_count=int(payload.get("restart_count") or 0),
        )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _CURRENT_VERSION.match(value.strip())
    if match is None:
        raise ValueError(f"Invalid DeepTutor version: {value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _normalise_stable_version(value: str) -> str:
    match = _STABLE_VERSION.fullmatch(value.strip())
    if match is None:
        raise ValueError("Update target must be a stable semantic version")
    return ".".join(match.groups())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _desktop_version_key(value: str) -> tuple[int, ...]:
    """Parse a four-segment desktop version into a comparable tuple.

    Mirrors desktop-shell ``updater.version_key``: leading ``dt`` prefixes,
    build timestamps and plain semantic versions all collapse to their numeric
    components. ``""``/garbage yields ``(0,)`` so an unparseable remote never
    beats a real installed version.
    """
    parts = re.findall(r"\d+", value or "")
    return tuple(int(part) for part in parts) or (0,)


def _desktop_is_newer(remote: str, local: str) -> bool:
    """Return whether ``remote`` is strictly newer than ``local``.

    Aligned with desktop-shell ``updater.is_newer``: a missing remote is never
    newer, while a missing local is treated as old so a first install prompts.
    """
    if not remote:
        return False
    if not local:
        return True
    return _desktop_version_key(remote) > _desktop_version_key(local)


def desktop_update_enabled() -> bool:
    """Whether the backend is running inside the desktop shell install shape."""
    return os.getenv(DESKTOP_SHELL_MANAGED_ENV, "").strip() == "1"


def _desktop_version() -> str:
    return os.getenv(DESKTOP_VERSION_ENV, "").strip()


def _desktop_mac_arch() -> str:
    return "arm64" if platform.machine() == "arm64" else "x86_64"


def _running_in_container() -> bool:
    configured = os.getenv("DEEPTUTOR_CONTAINER", "").strip().lower()
    return configured in {"1", "true", "yes", "on"} or any(
        marker.exists() for marker in (Path("/.dockerenv"), Path("/run/.containerenv"))
    )


def _distribution_direct_url() -> dict[str, Any] | None:
    try:
        distribution = importlib.metadata.distribution("deeptutor")
    except importlib.metadata.PackageNotFoundError:
        return None
    try:
        raw = distribution.read_text("direct_url.json")
        payload = json.loads(raw) if raw else {}
    except (OSError, ValueError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _running_from_source_checkout() -> bool:
    """Return whether the imported package lives in a Git source checkout."""
    try:
        checkout_root = Path(__file__).resolve().parents[2]
    except IndexError:
        return False
    return (checkout_root / ".git").exists() and (checkout_root / "pyproject.toml").is_file()


def _systemd_service_unit(cgroup_text: str) -> str | None:
    """Find the owning service unit, ignoring user managers and app scopes."""
    for line in cgroup_text.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        # The nearest unit owns the process. A desktop app scope commonly sits
        # below user@1000.service, but is not itself that service's process.
        for component in reversed(fields[2].split("/")):
            if component.endswith(".scope"):
                break
            if component.endswith(".service"):
                if component.startswith("user@"):
                    break
                return component
    return None


def running_under_systemd_service() -> bool:
    """A detached process group does not escape a systemd service cgroup."""
    if sys.platform != "linux":
        return False
    try:
        membership = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return bool(os.getenv("INVOCATION_ID"))
    if _systemd_service_unit(membership) is not None:
        return True
    # INVOCATION_ID can also be inherited by a desktop app scope. It is only
    # useful when a private cgroup namespace hides the actual unit path.
    visible_paths = [
        line.split(":", 2)[2] for line in membership.splitlines() if line.count(":") >= 2
    ]
    return bool(os.getenv("INVOCATION_ID")) and all(path in {"", "/"} for path in visible_paths)


def detect_installation() -> Installation:
    """Classify only layouts whose update ownership is unambiguous."""

    if _running_in_container():
        return Installation(
            mode="docker",
            current_version=__version__,
            automatic_update=False,
            command="docker pull ghcr.io/hkuds/deeptutor:latest",
            reason="Container images are updated and recreated by the Docker host.",
        )

    if _running_from_source_checkout():
        return Installation(
            mode="source",
            current_version=__version__,
            automatic_update=False,
            command="git pull && pip install -e .",
            reason="Source checkouts stay under the developer's Git workflow.",
        )

    direct_url = _distribution_direct_url()
    if direct_url is None:
        return Installation(
            mode="unknown",
            current_version=__version__,
            automatic_update=False,
            command="pip install -U deeptutor",
            reason="The running DeepTutor distribution could not be identified.",
        )
    if bool((direct_url.get("dir_info") or {}).get("editable")):
        return Installation(
            mode="source",
            current_version=__version__,
            automatic_update=False,
            command="git pull && pip install -e .",
            reason="Source checkouts stay under the developer's Git workflow.",
        )

    in_virtualenv = Path(sys.prefix).resolve() != Path(sys.base_prefix).resolve()
    if not direct_url and in_virtualenv:
        if running_under_systemd_service():
            return Installation(
                mode="pypi",
                current_version=__version__,
                automatic_update=False,
                command=f"{sys.executable} -m pip install -U deeptutor",
                reason=SYSTEMD_UPDATE_REASON,
            )
        return Installation(
            mode="pypi",
            current_version=__version__,
            automatic_update=True,
            command="pip install -U deeptutor",
            reason="",
        )
    return Installation(
        mode="unknown",
        current_version=__version__,
        automatic_update=False,
        command="pip install -U deeptutor",
        reason=(
            "Automatic updates require a regular PyPI installation in an active virtual environment."
            if not direct_url
            else "This installation came from a local or direct package artifact."
        ),
    )


class VersionCheckService:
    """Read the latest stable release with a process-local 24-hour cache."""

    def __init__(
        self,
        *,
        api_url: str = GITHUB_LATEST_RELEASE_URL,
        latest_url: str = GITHUB_LATEST_RELEASE_WEB_URL,
        timeout: float = 8.0,
        ttl_seconds: float = VERSION_CHECK_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._api_url = api_url
        self._latest_url = latest_url
        self._timeout = timeout
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._client_factory = client_factory
        self._cache: VersionCheckResult | None = None
        self._cached_at: float | None = None
        self._lock = asyncio.Lock()

    def cached(self) -> VersionCheckResult | None:
        if self._cache is None or self._cached_at is None:
            return None
        if self._clock() - self._cached_at >= self._ttl_seconds:
            return None
        return replace(self._cache, cached=True)

    async def check(self, *, force: bool = False) -> VersionCheckResult:
        async with self._lock:
            if not force and (cached := self.cached()) is not None:
                return cached
            release = await self._fetch()
            result = VersionCheckResult(
                current_version=__version__,
                release=release,
                checked_at=_now(),
                cached=False,
            )
            self._cache = result
            self._cached_at = self._clock()
            return result

    async def _fetch(self) -> ReleaseInfo:
        factory = self._client_factory or (
            lambda: httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=5.0),
                follow_redirects=True,
            )
        )
        async with factory() as client:
            try:
                response = await client.get(
                    self._api_url,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "User-Agent": "DeepTutor-Version-Check",
                    },
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                return await self._fetch_latest_redirect(client)
        return _release_from_payload(payload)

    async def _fetch_latest_redirect(self, client: httpx.AsyncClient) -> ReleaseInfo:
        """Resolve GitHub's rate-limit-free latest-release redirect."""

        try:
            response = await client.head(
                self._latest_url,
                headers={"User-Agent": "DeepTutor-Version-Check"},
                follow_redirects=False,
            )
            if response.is_redirect:
                location = response.headers.get("location", "").strip()
                if not location:
                    raise VersionCheckError("The latest stable release is unavailable")
                release_url = urljoin(str(response.url), location)
            else:
                response.raise_for_status()
                release_url = str(response.url)
        except httpx.HTTPError as exc:
            # The release URL is public and carries no credentials, so the
            # transport reason is safe to show — and it is the difference
            # between "try again" and "you are offline" / "your proxy blocked
            # github.com".
            raise VersionCheckError(f"Unable to check for updates: {exc}") from None
        return _release_from_latest_url(release_url)


class DesktopVersionCheckService:
    """Mirror the ThinkBuddy desktop release manifest with a 1-hour cache.

    This exists only for the sidebar badge: the desktop shell itself performs
    the authoritative check/download/install flow. Failure therefore degrades
    to "not available" rather than raising — an orange dot that does not light
    up is always preferable to a false update prompt.
    """

    def __init__(
        self,
        *,
        manifest_url: str = THINKBUDDY_UPDATE_MANIFEST_URL,
        timeout: float = 8.0,
        ttl_seconds: float = DESKTOP_VERSION_CHECK_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._manifest_url = manifest_url
        self._timeout = timeout
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._client_factory = client_factory
        self._cache: DesktopUpdateCheckResult | None = None
        self._cached_at: float | None = None
        self._lock = asyncio.Lock()

    def cached(self) -> DesktopUpdateCheckResult | None:
        if self._cache is None or self._cached_at is None:
            return None
        if self._clock() - self._cached_at >= self._ttl_seconds:
            return None
        return replace(self._cache, cached=True)

    async def check(self, *, force: bool = False) -> DesktopUpdateCheckResult:
        enabled = desktop_update_enabled()
        current_version = _desktop_version()
        # Without the desktop shell (browser/PyPI deployments) or a known shell
        # version there is nothing meaningful to compare — never hit the network.
        if not enabled or not current_version:
            return DesktopUpdateCheckResult(
                enabled=enabled,
                current_version=current_version,
                version="",
                url="",
                published_at="",
                checked_at="",
                cached=False,
                error="",
            )
        async with self._lock:
            if not force and (cached := self.cached()) is not None:
                return cached
            try:
                release = await self._fetch()
            except DesktopUpdateError as exc:
                result = DesktopUpdateCheckResult(
                    enabled=True,
                    current_version=current_version,
                    version="",
                    url="",
                    published_at="",
                    checked_at=_now(),
                    cached=False,
                    error=str(exc),
                )
            else:
                result = DesktopUpdateCheckResult(
                    enabled=True,
                    current_version=current_version,
                    version=release.version,
                    url=release.url,
                    published_at=release.published_at,
                    checked_at=_now(),
                    cached=False,
                    error="",
                )
            self._cache = result
            self._cached_at = self._clock()
            return result

    async def _fetch(self) -> DesktopReleaseInfo:
        factory = self._client_factory or (
            lambda: httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=5.0),
                follow_redirects=True,
            )
        )
        async with factory() as client:
            try:
                response = await client.get(
                    self._manifest_url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "DeepTutor-Desktop-Version-Check",
                    },
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise DesktopUpdateError(
                    f"Unable to check for desktop updates: {exc}"
                ) from None
        return _desktop_release_from_payload(payload)


def _desktop_release_from_payload(payload: Any) -> DesktopReleaseInfo:
    """Validate the ThinkBuddy latest.json and pick the current platform entry."""
    if not isinstance(payload, dict):
        raise DesktopUpdateError("The desktop update manifest is invalid")
    if payload.get("app") not in (None, "ThinkBuddy"):
        raise DesktopUpdateError("The desktop update manifest belongs to another app")

    entry = payload
    if sys.platform == "darwin":
        mac_section = payload.get("mac")
        if isinstance(mac_section, dict):
            arch = _desktop_mac_arch()
            entry = mac_section.get(arch)
            if not isinstance(entry, dict):
                raise DesktopUpdateError(
                    f"The desktop update manifest has no macOS {arch} build"
                )
        # A legacy single-arch mac manifest keeps its fields at the top level.

    version = entry.get("version")
    url = entry.get("url")
    if not (isinstance(version, str) and version.strip()) or not (
        isinstance(url, str) and url.strip()
    ):
        raise DesktopUpdateError(
            "The desktop update manifest is missing a version or download URL"
        )
    url = url.strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DesktopUpdateError(
            "The desktop update manifest has an invalid download URL"
        )
    published_at = entry.get("published_at") or payload.get("published_at") or ""
    return DesktopReleaseInfo(
        version=version.strip(),
        url=url,
        published_at=str(published_at).strip(),
    )


def _release_from_payload(payload: Any) -> ReleaseInfo:
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        raise VersionCheckError("The latest stable release is unavailable")
    try:
        version = _normalise_stable_version(str(payload.get("tag_name") or ""))
    except ValueError:
        raise VersionCheckError("The latest release has an invalid version") from None
    url = str(payload.get("html_url") or "").strip()
    if not url.startswith("https://github.com/HKUDS/DeepTutor/releases/"):
        raise VersionCheckError("The latest release has an invalid URL")
    body = str(payload.get("body") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    excerpt = _plain_release_excerpt(body)
    lowered = body.lower()
    migration_warning = bool(
        re.search(
            r"breaking chang|migration(?: needed| required)|database migration|run migration|migrate your",
            lowered,
        )
    )
    return ReleaseInfo(
        version=version,
        name=str(payload.get("name") or "").strip(),
        published_at=str(payload.get("published_at") or "").strip(),
        url=url,
        excerpt=excerpt,
        migration_warning=migration_warning,
    )


def _release_from_latest_url(value: str) -> ReleaseInfo:
    """Build minimal release metadata from GitHub's trusted latest redirect."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise VersionCheckError("The latest release has an invalid URL") from None
    prefix = "/HKUDS/DeepTutor/releases/tag/"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path.startswith(prefix)
    ):
        raise VersionCheckError("The latest release has an invalid URL")
    tag = unquote(parsed.path.removeprefix(prefix))
    if not tag or "/" in tag:
        raise VersionCheckError("The latest release has an invalid version")
    try:
        version = _normalise_stable_version(tag)
    except ValueError:
        raise VersionCheckError("The latest release has an invalid version") from None
    return ReleaseInfo(
        version=version,
        name=f"DeepTutor {version}",
        published_at="",
        url=f"https://github.com{parsed.path}",
        excerpt="",
        migration_warning=False,
    )


def _plain_release_excerpt(body: str) -> str:
    """Turn GitHub-flavoured release notes into a compact settings-page summary."""

    text = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s{0,3}#{1,6}\s+.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"<https?://[^>]+>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"(?m)^\s*(?:>|[-+*]|\d+[.)])\s+", "", text)
    text = re.sub(r"[*_~`]", "", text)

    lines: list[str] = []
    for line in text.splitlines():
        clean = re.sub(r"\s+", " ", line).strip()
        if re.match(r"^(?:release date|published)\s*:", clean, flags=re.IGNORECASE):
            continue
        if clean:
            lines.append(clean)
    summary = "\n\n".join(lines)
    return summary if len(summary) <= 640 else f"{summary[:637].rstrip()}..."


def update_store_root(home: str | Path | None = None) -> Path:
    return get_runtime_home(home) / "data" / "user" / "update"


class UpdateJobStore:
    """Persist one update job across the launcher restart boundary."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.state_path = self.root / "state.json"
        self.active_path = self.root / "active"
        self.log_path = self.root / "worker.log"

    def create(self, *, current_version: str, target_version: str) -> UpdateJob:
        current = _normalise_stable_version(current_version)
        target = _normalise_stable_version(target_version)
        if _version_tuple(target) <= _version_tuple(current):
            raise UpdateRequestError("No newer DeepTutor release is available")
        self.root.mkdir(parents=True, exist_ok=True)
        job = UpdateJob(
            id=uuid.uuid4().hex,
            status="pending",
            current_version=current,
            target_version=target,
            created_at=_now(),
        )
        try:
            descriptor = os.open(self.active_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise UpdateInProgressError("Another update is already in progress") from exc
        try:
            os.write(descriptor, job.id.encode("utf-8"))
        finally:
            os.close(descriptor)
        try:
            self._write(job)
        except Exception:
            self.release(job.id)
            raise
        return job

    def load(self) -> UpdateJob:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Invalid update job")
        return UpdateJob.from_dict(payload)

    def prepare_handoff(
        self,
        job_id: str,
        *,
        home: Path,
        restart_argv: Sequence[str],
    ) -> UpdateJob:
        current = self.load()
        resolved_home = str(home.resolve())
        if current.id != job_id or current.status != "pending":
            raise RuntimeError("Update job is not awaiting launcher handoff")
        updated = replace(
            current,
            status="handoff",
            restart_home=resolved_home,
            restart_argv=_validate_restart_argv(tuple(restart_argv), home=resolved_home),
        )
        self._write(updated)
        return updated

    def mark_running(self, job_id: str) -> UpdateJob:
        return self._transition(job_id, expected={"handoff"}, status="running")

    def mark_restarting(self, job_id: str) -> UpdateJob:
        current = self.load()
        if current.id != job_id or current.status != "running":
            raise RuntimeError("Update job is not ready to restart")
        updated = replace(
            current,
            status="restarting",
            restart_count=current.restart_count + 1,
        )
        self._write(updated)
        return updated

    def mark_succeeded(self, job_id: str) -> UpdateJob:
        return self._transition(job_id, expected={"restarting"}, status="succeeded")

    def mark_failed(self, job_id: str, error: str) -> UpdateJob:
        return self._transition(
            job_id,
            expected={"pending", "handoff", "running", "restarting"},
            status="failed",
            error=(error or "Update failed")[:1000],
        )

    def _transition(
        self,
        job_id: str,
        *,
        expected: set[JobStatus],
        status: JobStatus,
        error: str | None = None,
    ) -> UpdateJob:
        current = self.load()
        if current.id != job_id or current.status not in expected:
            raise RuntimeError("Update job changed while it was running")
        timestamp = _now()
        updated = replace(
            current,
            status=status,
            started_at=timestamp if status == "running" else current.started_at,
            finished_at=timestamp if status in {"succeeded", "failed"} else current.finished_at,
            error=error,
        )
        self._write(updated)
        if status in {"succeeded", "failed"}:
            self.release(job_id)
        return updated

    def release(self, job_id: str) -> None:
        try:
            owner = self.active_path.read_text(encoding="utf-8")
        except OSError:
            return
        if owner == job_id:
            self.active_path.unlink(missing_ok=True)

    def _write(self, job: UpdateJob) -> None:
        atomic_write_json(self.state_path, job.to_dict())


def _validate_restart_argv(value: object, *, home: str | None) -> tuple[str, ...]:
    if value in (None, (), []):
        return ()
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(argument, str) or not argument for argument in value
    ):
        raise ValueError("Invalid restart arguments")
    argv = tuple(value)
    valid = home is not None and argv[:3] == ("start", "--home", home)
    if not valid or argv[3:] not in {(), ("--dev",)}:
        raise ValueError("Invalid restart arguments")
    return argv


def launcher_available() -> bool:
    raw = os.getenv(LAUNCHER_PID_ENV, "").strip()
    try:
        pid = int(raw)
    except ValueError:
        return False
    return is_process_alive(pid)


def launch_update_worker(store_root: Path, *, parent_pid: int) -> None:
    """Launch the trusted worker outside the process tree being replaced."""

    command = [
        sys.executable,
        "-m",
        "deeptutor.runtime.update_worker",
        "--store-root",
        str(store_root.resolve()),
        "--parent-pid",
        str(parent_pid),
    ]
    store_root.mkdir(parents=True, exist_ok=True)
    log_path = store_root / "worker.log"
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        )
    else:
        kwargs["start_new_session"] = True
    with log_path.open("a", encoding="utf-8") as log:
        subprocess.Popen(command, stdout=log, **kwargs)  # nosec B603


_version_service = VersionCheckService()


def get_version_check_service() -> VersionCheckService:
    return _version_service


def reset_version_check_service_for_tests(service: VersionCheckService | None = None) -> None:
    global _version_service
    _version_service = service or VersionCheckService()


_desktop_version_service = DesktopVersionCheckService()


def get_desktop_version_check_service() -> DesktopVersionCheckService:
    return _desktop_version_service


def reset_desktop_version_check_service_for_tests(
    service: DesktopVersionCheckService | None = None,
) -> None:
    global _desktop_version_service
    _desktop_version_service = service or DesktopVersionCheckService()


__all__ = [
    "DesktopUpdateCheckResult",
    "DesktopUpdateError",
    "DesktopVersionCheckService",
    "Installation",
    "LAUNCHER_PID_ENV",
    "ReleaseInfo",
    "SYSTEMD_UPDATE_REASON",
    "UpdateInProgressError",
    "UpdateJob",
    "UpdateJobStore",
    "UpdateRequestError",
    "VersionCheckError",
    "VersionCheckResult",
    "VersionCheckService",
    "desktop_update_enabled",
    "detect_installation",
    "get_desktop_version_check_service",
    "get_version_check_service",
    "launch_update_worker",
    "launcher_available",
    "reset_desktop_version_check_service_for_tests",
    "reset_version_check_service_for_tests",
    "running_under_systemd_service",
    "update_store_root",
]
