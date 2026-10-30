from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mcpprobe.cli import app
from mcpprobe.exit_codes import ExitCode

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "mcpprobe" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["run"],
        ["run", "--stdio", "x", "--http", "https://localhost/mcp"],
        ["run", "--stdio", "x", "--header", "A: b"],
        ["run", "--http", "https://localhost/mcp", "--header", "no-colon"],
        ["run", "--stdio", "x", "--suites", "conf,bogus"],
        ["run", "--stdio", "x", "--suites", " , "],
        ["run", "--stdio", "x", "--baseline", "missing.json"],
        ["replay", "missing.json"],
    ],
)
def test_invalid_invocations_exit_3(args: list[str]) -> None:
    assert runner.invoke(app, args).exit_code == ExitCode.CONFIG_INVALID


def test_run_against_stdio_server(tmp_path: Path, stdio_argv: list[str]) -> None:
    command = " ".join(f'"{part}"' for part in stdio_argv)
    result = runner.invoke(
        app, ["run", "--stdio", command, "--seed", "42", "--cases-dir", str(tmp_path)]
    )
    assert result.exit_code == ExitCode.PASSED, result.output
    assert "seed 42" in result.output
    assert "not modern-era" in " ".join(result.output.split())


def test_run_unreachable_server_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "--http", "http://127.0.0.1:9/mcp", "--timeout", "1"])
    assert result.exit_code == ExitCode.PROBE_ERROR


@pytest.mark.parametrize(
    "extra",
    [["--report", "sarif"], ["-o", "probe.json"], ["--env", "A=b", "--http", "http://h"]],
)
def test_run_unsupported_options_exit_3(extra: list[str]) -> None:
    args = ["run", *extra] if "--http" in extra else ["run", "--stdio", "x", *extra]
    assert runner.invoke(app, args).exit_code == ExitCode.CONFIG_INVALID


def test_replay_rejects_invalid_recording(tmp_path: Path) -> None:
    case = tmp_path / "case.json"
    case.write_text("{}", encoding="utf-8")
    assert runner.invoke(app, ["replay", str(case)]).exit_code == ExitCode.CONFIG_INVALID


def test_record_then_replay_stdio(tmp_path: Path, stdio_argv: list[str]) -> None:
    script = tmp_path / "messages.jsonl"
    script.write_text(
        '# comment lines and blanks are skipped\n\n{"jsonrpc":"2.0","id":1,"method":"echo"}\n',
        encoding="utf-8",
    )
    recording = tmp_path / "out" / "rec.json"
    command = " ".join(f'"{part}"' for part in stdio_argv)
    recorded = runner.invoke(
        app,
        [
            "record",
            "--stdio",
            command,
            "--send",
            str(script),
            "-o",
            str(recording),
            "--env",
            "PROBE_SECRET=abcdef",
        ],
    )
    assert recorded.exit_code == 0, recorded.output
    assert "sent 1, answered 1" in recorded.output

    missing_env = runner.invoke(app, ["replay", str(recording)])
    assert missing_env.exit_code == ExitCode.CONFIG_INVALID

    again = tmp_path / "again.json"
    replayed = runner.invoke(
        app, ["replay", str(recording), "--env", "PROBE_SECRET=abcdef", "-o", str(again)]
    )
    assert replayed.exit_code == 0, replayed.output
    assert "replay matched" in replayed.output
    assert again.is_file()


def test_replay_reports_differences(tmp_path: Path, stdio_argv: list[str]) -> None:
    script = tmp_path / "messages.jsonl"
    script.write_text('{"jsonrpc":"2.0","id":1,"method":"echo"}\n', encoding="utf-8")
    recording = tmp_path / "rec.json"
    command = " ".join(f'"{part}"' for part in stdio_argv)
    assert (
        runner.invoke(
            app, ["record", "--stdio", command, "--send", str(script), "-o", str(recording)]
        ).exit_code
        == 0
    )
    recording.write_text(
        recording.read_text(encoding="utf-8").replace('\\"echo\\":null', '\\"echo\\":42'),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["replay", str(recording)])
    assert result.exit_code == ExitCode.FAILED, result.output


def test_record_http_with_warning(tmp_path: Path, http_url: str) -> None:
    script = tmp_path / "messages.jsonl"
    script.write_text('{"jsonrpc":"2.0","id":1,"method":"echo"}\n', encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "record",
            "--http",
            http_url,
            "--send",
            str(script),
            "-o",
            str(tmp_path / "r.json"),
            "--header",
            "Authorization: Bearer nope-nope",
        ],
    )
    assert result.exit_code == 0
    assert "HTTP 401" in result.output


def test_record_unstartable_server_exits_2(tmp_path: Path) -> None:
    script = tmp_path / "m.jsonl"
    script.write_text("{}\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "record",
            "--stdio",
            "no-such-program-xyz",
            "--send",
            str(script),
            "-o",
            str(tmp_path / "r.json"),
        ],
    )
    assert result.exit_code == ExitCode.PROBE_ERROR


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--stdio", "x", "--http", "http://h"],
        ["--stdio", "x", "--header", "A: b"],
        ["--http", "http://h", "--env", "A=b"],
        ["--http", "http://h", "--header", "bad"],
        ["--stdio", "x", "--env", "bad"],
        ["--stdio", ""],
    ],
)
def test_record_invalid_invocations(tmp_path: Path, extra: list[str]) -> None:
    script = tmp_path / "m.jsonl"
    script.write_text("{}\n", encoding="utf-8")
    args = ["record", "--send", str(script), "-o", str(tmp_path / "r.json"), *extra]
    assert runner.invoke(app, args).exit_code == ExitCode.CONFIG_INVALID


def test_record_empty_or_missing_script(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n# nothing\n", encoding="utf-8")
    for script in (empty, tmp_path / "missing.jsonl"):
        args = ["record", "--stdio", "x", "--send", str(script), "-o", str(tmp_path / "r.json")]
        assert runner.invoke(app, args).exit_code == ExitCode.CONFIG_INVALID


def test_checks_list_and_explain_real_catalog() -> None:
    listed = runner.invoke(app, ["checks", "list"])
    assert listed.exit_code == 0
    assert "CONF-001" in listed.output
    explained = runner.invoke(app, ["checks", "explain", "CONF-006"])
    assert explained.exit_code == 0
    assert "streamable-http#server-validation" in "".join(explained.output.split())
    assert runner.invoke(app, ["checks", "explain", "CONF-999"]).exit_code == 3


def test_checks_list_and_explain_with_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcpprobe import cli
    from mcpprobe.engine import catalog

    data = Path(__file__).parent / "data" / "checks" / "valid"
    monkeypatch.setattr(cli, "load_catalog", lambda: catalog.load_catalog(data))

    listed = runner.invoke(app, ["checks", "list", "--suite", "sec"])
    assert listed.exit_code == 0
    assert "SEC-901" in listed.output
    assert "CONF-901" not in listed.output

    explained = runner.invoke(app, ["checks", "explain", "conf-901"])
    assert explained.exit_code == 0
    assert "modelcontextprotocol.io" in explained.output


def test_broken_catalog_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcpprobe import cli
    from mcpprobe.engine import catalog

    data = Path(__file__).parent / "data" / "checks" / "invalid"
    monkeypatch.setattr(cli, "load_catalog", lambda: catalog.load_catalog(data))
    assert runner.invoke(app, ["checks", "list"]).exit_code == ExitCode.CONFIG_INVALID


def test_silent_server_is_flagged(tmp_path: Path, stdio_argv: list[str]) -> None:
    script = tmp_path / "messages.jsonl"
    script.write_text('{"jsonrpc":"2.0","id":1,"method":"silent"}\n', encoding="utf-8")
    recording = tmp_path / "rec.json"
    command = " ".join(f'"{part}"' for part in stdio_argv)
    recorded = runner.invoke(
        app,
        [
            "record",
            "--stdio",
            command,
            "--send",
            str(script),
            "-o",
            str(recording),
            "--timeout",
            "0.5",
        ],
    )
    assert recorded.exit_code == 0
    assert "answered nothing" in recorded.output
    replayed = runner.invoke(app, ["replay", str(recording), "--timeout", "0.5"])
    assert replayed.exit_code == ExitCode.FAILED
    assert "trivially" in replayed.output


def test_run_json_report(tmp_path: Path) -> None:
    import json
    import sys

    reference = Path(__file__).parents[1] / "fixtures" / "servers" / "reference.py"
    command = f'"{sys.executable}" -u "{reference}" --break CONF-004'
    report = tmp_path / "probe.json"
    result = runner.invoke(
        app,
        [
            "run",
            "--stdio",
            command,
            "--seed",
            "1",
            "--report",
            "json",
            "-o",
            str(report),
            "--cases-dir",
            str(tmp_path / "cases"),
        ],
    )
    assert result.exit_code == ExitCode.FAILED, result.output
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["format"] == "mcpprobe.report"
    assert data["era"] == "modern"
    failed = [r for r in data["results"] if r["status"] == "failed"]
    assert [r["id"] for r in failed] == ["CONF-004"]
    assert failed[0]["spec_ref"].startswith("https://modelcontextprotocol.io/")

    to_stdout = runner.invoke(
        app, ["run", "--stdio", command, "--report", "json", "--cases-dir", str(tmp_path / "c")]
    )
    assert json.loads(to_stdout.output)["exit_code"] == 1
