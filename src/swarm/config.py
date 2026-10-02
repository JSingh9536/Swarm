"""Run configuration (`swarm.toml`) with safe defaults."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from swarm.mcp import CATALOG, DEFAULT_ENABLED
from swarm.roles import Role, repo_root


class ConfigError(ValueError):
    """swarm.toml is invalid."""


@dataclass
class Config:
    budget_usd: float = 3.0
    per_call_fraction: float = 0.4
    max_fix_rounds: int = 3
    research: bool = True
    role_timeout_s: float = 1800.0
    gate_timeout_s: float = 600.0
    commit: bool = False
    allow_api_billing: bool = False
    plan_stop_at: float = 0.9
    # Roles that pin no model (e.g. `model: inherit`) run on this, never on the account default, which can be the
    # largest model and would use a Claude plan up much faster. `[models] default = ""` restores the account default.
    default_model: str | None = "sonnet"
    force_model: str | None = None
    models: dict[str, str] = field(default_factory=dict)
    turns: dict[str, int] = field(default_factory=dict)
    mcp: tuple[str, ...] = DEFAULT_ENABLED
    cli_path: str | None = None
    roles_dir: Path | None = None
    workspace_dir: Path = field(default_factory=lambda: repo_root() / "workspace")

    def model_for(self, role: Role) -> str | None:
        return self.force_model or self.models.get(role.name) or role.model or self.default_model

    def turns_for(self, role: Role) -> int | None:
        return self.turns.get(role.name) or role.max_turns

    def call_budget(self, spent: float) -> float:
        """Budget for the next agent call: a slice of the total, never more than what is left."""
        return max(0.0, min(self.budget_usd - spent, max(0.5, self.budget_usd * self.per_call_fraction)))


_RUN_KEYS = {
    "budget_usd": float,
    "per_call_fraction": float,
    "max_fix_rounds": int,
    "research": bool,
    "role_timeout_s": float,
    "gate_timeout_s": float,
    "commit": bool,
    "allow_api_billing": bool,
    "plan_stop_at": float,
}


def _check_type(section: str, key: str, value: object, expected: type) -> object:
    if expected is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        raise ConfigError(f"[{section}] {key} must be {expected.__name__}, got {value!r}")
    return value


def from_dict(data: dict) -> Config:
    cfg = Config()
    unknown = set(data) - {"run", "models", "turns", "mcp", "claude"}
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown))}")

    for key, value in data.get("run", {}).items():
        if key not in _RUN_KEYS:
            raise ConfigError(f"[run] unknown key {key!r}; valid: {', '.join(_RUN_KEYS)}")
        setattr(cfg, key, _check_type("run", key, value, _RUN_KEYS[key]))
    if cfg.budget_usd <= 0:
        raise ConfigError("[run] budget_usd must be positive")
    if not 0 < cfg.per_call_fraction <= 1:
        raise ConfigError("[run] per_call_fraction must be in (0, 1]")
    if not 0 < cfg.plan_stop_at <= 1:
        raise ConfigError("[run] plan_stop_at must be in (0, 1]")
    if cfg.max_fix_rounds < 0:
        raise ConfigError("[run] max_fix_rounds must be >= 0")

    for key, value in data.get("models", {}).items():
        _check_type("models", key, value, str)
        if key == "default":
            cfg.default_model = value or None
        elif key == "force":
            cfg.force_model = value or None
        else:
            cfg.models[key] = value

    for key, value in data.get("turns", {}).items():
        cfg.turns[key] = int(_check_type("turns", key, value, int))

    mcp = data.get("mcp", {})
    for key in mcp:
        if key != "enabled":
            raise ConfigError(f"[mcp] unknown key {key!r}; valid: enabled")
    if "enabled" in mcp:
        names = mcp["enabled"]
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ConfigError("[mcp] enabled must be a list of names")
        bad = [n for n in names if n not in CATALOG]
        if bad:
            raise ConfigError(f"[mcp] unknown server(s) {bad}; known: {', '.join(CATALOG)}")
        cfg.mcp = tuple(names)

    claude = data.get("claude", {})
    for key, value in claude.items():
        if key != "cli_path":
            raise ConfigError(f"[claude] unknown key {key!r}; valid: cli_path")
        cfg.cli_path = str(_check_type("claude", key, value, str)) or None
    return cfg


def load_config(project_dir: Path | None = None, explicit: Path | None = None) -> Config:
    """Read the first `swarm.toml` found: explicit path, project directory, then the swarm checkout."""
    if explicit is not None and not explicit.is_file():
        raise ConfigError(f"config file not found: {explicit}")
    candidates = [explicit] if explicit else []
    if project_dir:
        candidates.append(project_dir / "swarm.toml")
    candidates.append(repo_root() / "swarm.toml")
    for path in candidates:
        if path and path.is_file():
            try:
                return from_dict(tomllib.loads(path.read_text(encoding="utf-8")))
            except (tomllib.TOMLDecodeError, ConfigError) as exc:
                raise ConfigError(f"{path}: {exc}") from exc
    return Config()
