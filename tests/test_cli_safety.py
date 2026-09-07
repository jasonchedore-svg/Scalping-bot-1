from __future__ import annotations

from typer.testing import CliRunner

from scalping_bot.cli import app


def test_start_live_is_hard_gated() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["start", "--live"])
    assert result.exit_code == 2
    combined = (result.output or "") + str(result.exception or "")
    assert "hard-gated" in combined.lower() or "Live trading" in combined
