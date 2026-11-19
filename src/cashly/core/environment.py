"""Select one environment source explicitly; never merge multiple dotenv files."""

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENVIRONMENTS = {"dev", "staging", "prod"}
CI_BRANCH_KEYS = (
    "GITHUB_HEAD_REF",
    "GITHUB_REF_NAME",
    "CI_COMMIT_REF_NAME",
    "VERCEL_GIT_COMMIT_REF",
    "BUILD_SOURCEBRANCH",
)
LEGACY_FILES = (
    ".env",
    ".env.local",
    ".env.development",
    ".env.development.local",
    ".env.production",
    ".env.production.local",
    ".env.staging.local",
)


class ConfigurationError(ValueError):
    """An actionable configuration error containing no configuration values."""


@dataclass(frozen=True)
class EnvironmentSelection:
    environment: str
    source: str
    filename: str
    # Never print loaded values; repr is intentionally metadata-only.
    values: Mapping[str, str] = field(repr=False)


def branch_environment(branch: str) -> str:
    name = branch.removeprefix("refs/heads/").lower()
    if name in {"main", "master", "prod", "production"}:
        return "prod"
    if name in {"stage", "staging"}:
        return "staging"
    return "dev"


def detect_branch(root: Path, env: Mapping[str, str]) -> str | None:
    # Tags are not deployment branches, even when a tag happens to be named main.
    if env.get("GITHUB_REF_TYPE") == "tag" or env.get("CI_COMMIT_TAG"):
        return None
    for key in CI_BRANCH_KEYS:
        if branch := env.get(key, "").strip():
            if branch.startswith("refs/tags/"):
                return None
            return branch
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={root.as_posix()}",
                "symbolic-ref",
                "--quiet",
                "--short",
                "HEAD",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result.stdout.strip() or None if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def resolve_environment(
    *,
    root: Path = PROJECT_ROOT,
    env: Mapping[str, str] | None = None,
    override: str | None = None,
    source_override: str | None = None,
) -> EnvironmentSelection:
    inherited = dict(os.environ if env is None else env)
    explicit = override if override is not None else inherited.get("CASHLY_ENV")
    if explicit is not None and explicit not in ENVIRONMENTS:
        raise ConfigurationError("CASHLY_ENV / --app-env must be dev, staging or prod.")
    branch = detect_branch(root, inherited) if explicit is None else None
    selected = explicit or (branch_environment(branch) if branch else None)
    if selected is None:
        raise ConfigurationError("No branch available; set CASHLY_ENV or --app-env explicitly.")
    source = (
        source_override
        if source_override is not None
        else inherited.get("CASHLY_ENV_SOURCE", "file")
    )
    if source not in {"file", "system"}:
        raise ConfigurationError("CASHLY_ENV_SOURCE / --env-source must be file or system.")
    filename = f".env.{selected}"
    loaded: dict[str, str] = {}
    if source == "file":
        if any((root / name).exists() for name in LEGACY_FILES):
            raise ConfigurationError("Legacy env file detected; use only .env.dev/staging/prod.")
        if not (root / filename).is_file():
            raise ConfigurationError(
                f"{filename} missing; copy {filename}.example or select system source."
            )
        try:
            # No ${...} expansion: keep secrets literal and sources independent.
            loaded = {
                key: value
                for key, value in dotenv_values(
                    root / filename, encoding="utf-8-sig", interpolate=False
                ).items()
                if value is not None
            }
        except (OSError, UnicodeError):
            raise ConfigurationError("Selected env file cannot be read.") from None
    values = {**loaded, **inherited, "CASHLY_ENV": selected, "CASHLY_ENV_SOURCE": source}
    return EnvironmentSelection(selected, source, filename, values)
