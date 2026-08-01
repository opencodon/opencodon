"""Regression for #68523 — one systemctl timeout must not abort fleet restarts.

On hosts with many profile-backed ``opencodon-gateway*.service`` units,
``opencodon update`` used to wrap the entire per-scope unit loop in a single
``except subprocess.TimeoutExpired``. A timeout on unit N skipped units
N+1…, leaving later gateways on pre-update in-memory modules while the
checkout on disk was already new (mixed-generation crashes).
"""

from __future__ import annotations

import subprocess

import pytest

from opencodon.frontends.cli.main import (
    _for_each_systemd_gateway_unit,
    _warn_incomplete_gateway_fleet_restart,
)


def _list_units_stdout(names: list[str]) -> str:
    return "\n".join(f"{name}.service loaded active running" for name in names)


class TestFleetRestartTimeoutIsolation:
    def test_timeout_on_middle_unit_continues_remaining_units(self):
        units = [
            "opencodon-gateway-xiaomo1",
            "opencodon-gateway-xiaomo2",
            "opencodon-gateway-xiaomo3",
            "opencodon-gateway-xiaomo4",
            "opencodon-gateway-xiaomo5",
            "opencodon-gateway-xiaomo6",
            "opencodon-gateway-xiaomo7",
            "opencodon-gateway",
        ]
        restarted: list[str] = []
        failed: list[str] = []
        timeout_cmds: list = []

        def process_unit(svc_name: str) -> None:
            if svc_name == "opencodon-gateway-xiaomo5":
                raise subprocess.TimeoutExpired(
                    cmd=["systemctl", "--user", "--no-ask-password", "restart", svc_name],
                    timeout=15,
                )
            restarted.append(svc_name)

        def on_unit_timeout(svc_name: str, exc: subprocess.TimeoutExpired) -> None:
            failed.append(svc_name)
            timeout_cmds.append(exc.cmd)

        _for_each_systemd_gateway_unit(
            _list_units_stdout(units),
            process_unit=process_unit,
            on_unit_timeout=on_unit_timeout,
        )

        assert failed == ["opencodon-gateway-xiaomo5"]
        assert restarted == [
            "opencodon-gateway-xiaomo1",
            "opencodon-gateway-xiaomo2",
            "opencodon-gateway-xiaomo3",
            "opencodon-gateway-xiaomo4",
            "opencodon-gateway-xiaomo6",
            "opencodon-gateway-xiaomo7",
            "opencodon-gateway",
        ]
        assert set(restarted) | set(failed) == set(units)
        assert timeout_cmds == [
            ["systemctl", "--user", "--no-ask-password", "restart", "opencodon-gateway-xiaomo5"]
        ]

    def test_non_gateway_units_in_list_output_are_ignored(self):
        seen: list[str] = []

        _for_each_systemd_gateway_unit(
            "\n".join(
                [
                    "ssh.service loaded active running",
                    "opencodon-gateway-coder.service loaded active running",
                    "not-a-service loaded active running",
                    "",
                ]
            ),
            process_unit=seen.append,
            on_unit_timeout=lambda *_: pytest.fail("unexpected timeout"),
        )

        assert seen == ["opencodon-gateway-coder"]

    def test_process_errors_other_than_timeout_still_propagate(self):
        def process_unit(_svc_name: str) -> None:
            raise RuntimeError("not a timeout")

        with pytest.raises(RuntimeError, match="not a timeout"):
            _for_each_systemd_gateway_unit(
                _list_units_stdout(["opencodon-gateway"]),
                process_unit=process_unit,
                on_unit_timeout=lambda *_: pytest.fail("timeout handler must not run"),
            )


class TestIncompleteFleetRestartWarning:
    def test_warns_with_exact_unrestarted_units(self, capsys):
        _warn_incomplete_gateway_fleet_restart(
            ["opencodon-gateway-xiaomo5", "opencodon-gateway-xiaomo6", "opencodon-gateway-xiaomo5"]
        )
        out = capsys.readouterr().out
        assert "Update incomplete" in out
        assert out.count("opencodon-gateway-xiaomo5") == 1
        assert "opencodon-gateway-xiaomo6" in out
        assert "pre-update code" in out

    def test_noop_when_no_failures(self, capsys):
        _warn_incomplete_gateway_fleet_restart([])
        assert capsys.readouterr().out == ""
