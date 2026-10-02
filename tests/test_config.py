from __future__ import annotations

from pathlib import Path

import pytest

import swarm.config as config_mod
from swarm.config import Config, ConfigError, from_dict, load_config
from swarm.mcp import DEFAULT_ENABLED
from swarm.roles import Role


def role(name: str = "developer", model: str | None = None, max_turns: int | None = None) -> Role:
    return Role(name, "d", "prompt\n", ("Read",), model, max_turns, Path(f"{name}.md"))


# --------------------------------------------------------------------------- from_dict


def test_empty_dict_gives_defaults() -> None:
    cfg = from_dict({})
    assert cfg == Config()
    assert cfg.mcp == DEFAULT_ENABLED
    assert cfg.commit is False


def test_full_valid_dict() -> None:
    cfg = from_dict(
        {
            "run": {"budget_usd": 10, "per_call_fraction": 0.25, "max_fix_rounds": 2, "research": False,
                    "role_timeout_s": 60, "gate_timeout_s": 30.5, "commit": True},
            "models": {"default": "sonnet", "force": "", "architect": "opus"},
            "turns": {"developer": 20},
            "mcp": {"enabled": ["grep"]},
            "claude": {"cli_path": "C:/tools/claude.exe"},
        }
    )  # fmt: skip
    assert cfg.budget_usd == 10.0 and isinstance(cfg.budget_usd, float)
    assert cfg.per_call_fraction == 0.25
    assert cfg.max_fix_rounds == 2
    assert cfg.research is False and cfg.commit is True
    assert cfg.role_timeout_s == 60.0 and cfg.gate_timeout_s == 30.5
    assert cfg.default_model == "sonnet"
    assert cfg.force_model is None
    assert cfg.models == {"architect": "opus"}
    assert cfg.turns == {"developer": 20}
    assert cfg.mcp == ("grep",)
    assert cfg.cli_path == "C:/tools/claude.exe"


def test_mcp_can_be_disabled() -> None:
    assert from_dict({"mcp": {"enabled": []}}).mcp == ()


def test_empty_cli_path_is_none() -> None:
    assert from_dict({"claude": {"cli_path": ""}}).cli_path is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"extras": {}}, "unknown section"),
        ({"run": {"budget": 5}}, "unknown key 'budget'"),
        ({"run": {"budget_usd": "5"}}, "budget_usd must be float"),
        ({"run": {"budget_usd": True}}, "budget_usd must be float"),
        ({"run": {"max_fix_rounds": 1.5}}, "max_fix_rounds must be int"),
        ({"run": {"max_fix_rounds": True}}, "max_fix_rounds must be int"),
        ({"run": {"research": "yes"}}, "research must be bool"),
        ({"run": {"budget_usd": 0}}, "budget_usd must be positive"),
        ({"run": {"budget_usd": -1.0}}, "budget_usd must be positive"),
        ({"run": {"per_call_fraction": 0}}, "per_call_fraction"),
        ({"run": {"per_call_fraction": 1.5}}, "per_call_fraction"),
        ({"run": {"max_fix_rounds": -1}}, "max_fix_rounds must be >= 0"),
        ({"models": {"developer": 3}}, "must be str"),
        ({"turns": {"developer": "10"}}, "must be int"),
        ({"turns": {"developer": False}}, "must be int"),
        ({"mcp": {"servers": ["grep"]}}, "unknown key 'servers'"),
        ({"mcp": {"enabled": "grep"}}, "list of names"),
        ({"mcp": {"enabled": ["grep", 1]}}, "list of names"),
        ({"mcp": {"enabled": ["github"]}}, "unknown server"),
        ({"claude": {"path": "x"}}, "unknown key 'path'"),
        ({"claude": {"cli_path": 5}}, "cli_path must be str"),
    ],
)
def test_invalid_dicts(data: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        from_dict(data)


# --------------------------------------------------------------------------- per-role settings


def test_model_precedence() -> None:
    r = role("architect", model="opus-role")
    assert Config().model_for(role("x")) == "sonnet"  # safe default, never the account's largest model
    assert Config(default_model="dflt").model_for(role("x")) == "dflt"
    assert Config(default_model="dflt").model_for(r) == "opus-role"
    assert Config(default_model="dflt", models={"architect": "cfg"}).model_for(r) == "cfg"
    assert Config(default_model="dflt", models={"architect": "cfg"}, force_model="forced").model_for(r) == "forced"


def test_default_model_is_sonnet_not_account_default() -> None:
    # an unpinned role (model=None, e.g. `model: inherit`) must never fall through to the account default
    assert Config().model_for(role("x", model=None)) == "sonnet"
    assert Config().model_for(role("x", model="haiku")) == "haiku"


def test_default_model_escape_hatch() -> None:
    # an explicit empty default re-opens the account default on purpose
    cfg = from_dict({"models": {"default": ""}})
    assert cfg.default_model is None
    assert cfg.model_for(role("x", model=None)) is None
    assert cfg.model_for(role("x", model="haiku")) == "haiku"


def test_turns_precedence() -> None:
    assert Config().turns_for(role(max_turns=None)) is None
    assert Config().turns_for(role(max_turns=40)) == 40
    assert Config(turns={"developer": 5}).turns_for(role(max_turns=40)) == 5
    assert Config(turns={"tester": 5}).turns_for(role(max_turns=40)) == 40


def test_call_budget_is_a_slice() -> None:
    cfg = Config(budget_usd=6.0, per_call_fraction=0.4)
    assert cfg.call_budget(0) == pytest.approx(2.4)
    assert cfg.call_budget(5.0) == pytest.approx(1.0)
    assert cfg.call_budget(6.0) == 0.0
    assert cfg.call_budget(9.0) == 0.0


def test_call_budget_has_a_floor_but_never_exceeds_remaining() -> None:
    cfg = Config(budget_usd=1.0, per_call_fraction=0.1)
    assert cfg.call_budget(0) == pytest.approx(0.5)
    assert cfg.call_budget(0.8) == pytest.approx(0.2)


@pytest.mark.parametrize("spent", [0, 0.1, 1, 2.5, 3.99, 4, 7])
@pytest.mark.parametrize("budget", [0.3, 1, 4, 50])
def test_call_budget_never_exceeds_remaining(budget: float, spent: float) -> None:
    got = Config(budget_usd=budget).call_budget(spent)
    assert 0 <= got <= max(0.0, budget - spent) + 1e-9


# --------------------------------------------------------------------------- load_config


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(config_mod, "repo_root", lambda: repo)
    return repo


def toml(path: Path, budget: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"[run]\nbudget_usd = {budget}\n", encoding="utf-8")
    return path


def test_load_defaults_when_nothing_found(fake_repo: Path, tmp_path: Path) -> None:
    assert load_config(tmp_path / "project") == Config()
    assert load_config() == Config()


def test_load_search_order(fake_repo: Path, tmp_path: Path) -> None:
    project = tmp_path / "project"
    explicit = toml(tmp_path / "custom.toml", 1)
    toml(project / "swarm.toml", 2)
    toml(fake_repo / "swarm.toml", 3)

    assert load_config(project, explicit).budget_usd == 1
    assert load_config(project).budget_usd == 2
    assert load_config(tmp_path / "elsewhere").budget_usd == 3
    assert load_config().budget_usd == 3


def test_load_missing_explicit(fake_repo: Path, tmp_path: Path) -> None:
    toml(fake_repo / "swarm.toml", 3)
    with pytest.raises(ConfigError, match="not found"):
        load_config(explicit=tmp_path / "nope.toml")


def test_load_toml_syntax_error(fake_repo: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[run\nbudget_usd = \n", encoding="utf-8")
    with pytest.raises(ConfigError, match="bad.toml"):
        load_config(explicit=bad)


def test_load_invalid_values_name_the_file(fake_repo: Path, tmp_path: Path) -> None:
    project = tmp_path / "project"
    toml(project / "swarm.toml", -1)
    with pytest.raises(ConfigError, match=r"swarm\.toml.*budget_usd must be positive"):
        load_config(project)


def test_shipped_swarm_toml_is_valid() -> None:
    shipped = config_mod.repo_root() / "swarm.toml"
    if not shipped.is_file():
        pytest.skip("no swarm.toml in the checkout")
    assert load_config(explicit=shipped).default_model == "sonnet"
