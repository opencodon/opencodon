"""Tests for the `no model selected` startup guard in `opencodon chat`.

Credentials and model selection are written by different flows
(``opencodon auth`` / ``opencodon setup`` vs ``opencodon model``), so a
config.yaml holding an auth login but no ``model:`` key is reachable.  Before
this guard it sailed past the provider check and booted an agent whose model
was the empty string — the status bar read ``unknown`` / ``ctx --`` and the
first turn failed with no useful error.
"""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def main_mod(monkeypatch):
    import opencodon_cli.main as mod

    # Credentials present in every test here — these cover the *second* guard.
    monkeypatch.setattr(mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(mod, "_sync_bundled_skills_for_startup", lambda: None)
    monkeypatch.setattr(mod, "_termux_should_prefetch_update_check", lambda: False)
    monkeypatch.setattr(mod, "_oneshot_cleanup_done", False)
    return mod


@pytest.fixture
def launched(monkeypatch):
    """Record whether the chat REPL was actually reached."""
    seen: dict[str, object] = {}
    fake_cli = types.ModuleType("cli")
    setattr(fake_cli, "main", lambda **kwargs: seen.update(kwargs) or seen.setdefault("_ran", True))
    monkeypatch.setitem(sys.modules, "cli", fake_cli)
    return seen


def _chat_args(main_mod, argv):
    from opencodon_cli._parser import build_top_level_parser

    parser, _subparsers, chat_parser = build_top_level_parser()
    chat_parser.set_defaults(func=main_mod.cmd_chat)
    return parser.parse_args(argv)


# --- _configured_model_name / _has_model_configured -------------------------


@pytest.mark.parametrize(
    "cfg,expected",
    [
        ({}, ""),
        ({"model": None}, ""),
        ({"model": ""}, ""),
        ({"model": "   "}, ""),
        ({"model": 42}, ""),
        ({"model": "gpt-5.3-codex"}, "gpt-5.3-codex"),
        ({"model": {"default": "gpt-5.3-codex"}}, "gpt-5.3-codex"),
        # cli.py falls back to the `model` key when `default` is absent/blank.
        ({"model": {"model": "gpt-5.3-codex"}}, "gpt-5.3-codex"),
        ({"model": {"default": "", "model": "gpt-5.3-codex"}}, "gpt-5.3-codex"),
        ({"model": {"default": "", "provider": "openai-codex"}}, ""),
    ],
)
def test_configured_model_name(cfg, expected):
    from opencodon_cli.main import _configured_model_name

    assert _configured_model_name(cfg) == expected


@pytest.mark.parametrize(
    "cfg,expected",
    [
        ({}, False),
        ({"model": ""}, False),
        # An auth login with no model — the state this guard exists for.
        ({"model": {"provider": "openai-codex"}}, False),
        ({"model": "gpt-5.3-codex"}, True),
        ({"model": {"default": "gpt-5.3-codex"}}, True),
        # Local servers: cli.py auto-detects the served model, so a localhost
        # base_url is enough even with no model name.
        ({"model": {"base_url": "http://localhost:8000/v1"}}, True),
        ({"model": {"base_url": "http://127.0.0.1:11434/v1"}}, True),
        # A remote base_url gets no such auto-detect.
        ({"model": {"base_url": "https://api.example.com/v1"}}, False),
    ],
)
def test_has_model_configured(cfg, expected):
    from opencodon_cli.main import _has_model_configured

    assert _has_model_configured(cfg) is expected


# --- the cmd_chat guard -----------------------------------------------------


def test_chat_stops_when_no_model_is_configured(main_mod, launched, monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "_has_model_configured", lambda cfg=None: False)
    monkeypatch.setattr("opencodon_cli.setup.is_interactive_stdin", lambda: False)

    with pytest.raises(SystemExit) as exc:
        main_mod.cmd_chat(_chat_args(main_mod, ["chat"]))

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "No model is selected" in out
    assert "opencodon model" in out
    assert "_ran" not in launched


def test_chat_proceeds_when_a_model_is_configured(main_mod, launched, monkeypatch):
    monkeypatch.setattr(main_mod, "_has_model_configured", lambda cfg=None: True)

    main_mod.cmd_chat(_chat_args(main_mod, ["chat", "--cli"]))

    assert launched["_ran"] is True


def test_explicit_model_flag_bypasses_the_guard(main_mod, launched, monkeypatch):
    """--model supplies the model cmd_chat would otherwise read from config."""
    monkeypatch.setattr(main_mod, "_has_model_configured", lambda cfg=None: False)

    main_mod.cmd_chat(_chat_args(main_mod, ["chat", "--cli", "--model", "gpt-5.3-codex"]))

    assert launched["_ran"] is True


def test_declining_the_picker_exits_without_launching(main_mod, launched, monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "_has_model_configured", lambda cfg=None: False)
    monkeypatch.setattr("opencodon_cli.setup.is_interactive_stdin", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

    with pytest.raises(SystemExit) as exc:
        main_mod.cmd_chat(_chat_args(main_mod, ["chat"]))

    assert exc.value.code == 1
    assert "You can run 'opencodon model' at any time" in capsys.readouterr().out
    assert "_ran" not in launched


def test_accepting_runs_the_picker_then_launches(main_mod, launched, monkeypatch):
    picked = {"done": False}
    # The picker writes config.yaml; model then resolves on the re-check.
    monkeypatch.setattr(main_mod, "_has_model_configured", lambda cfg=None: picked["done"])
    monkeypatch.setattr("opencodon_cli.setup.is_interactive_stdin", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    monkeypatch.setattr(
        main_mod, "select_provider_and_model", lambda *a, **k: picked.update(done=True)
    )

    main_mod.cmd_chat(_chat_args(main_mod, ["chat", "--cli"]))

    assert picked["done"] is True
    assert launched["_ran"] is True


def test_picker_that_selects_nothing_still_exits(main_mod, launched, monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "_has_model_configured", lambda cfg=None: False)
    monkeypatch.setattr("opencodon_cli.setup.is_interactive_stdin", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    monkeypatch.setattr(main_mod, "select_provider_and_model", lambda *a, **k: None)

    with pytest.raises(SystemExit) as exc:
        main_mod.cmd_chat(_chat_args(main_mod, ["chat"]))

    assert exc.value.code == 1
    assert "Still no model selected" in capsys.readouterr().out
    assert "_ran" not in launched


def test_missing_credentials_still_takes_the_setup_path(main_mod, monkeypatch, capsys):
    """The original first-run guard wins when there are no credentials at all."""
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: False)
    monkeypatch.setattr(main_mod, "_has_model_configured", lambda cfg=None: False)
    monkeypatch.setattr("opencodon_cli.setup.is_interactive_stdin", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

    with pytest.raises(SystemExit) as exc:
        main_mod.cmd_chat(_chat_args(main_mod, ["chat"]))

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "isn't configured yet" in out
    assert "No model is selected" not in out
