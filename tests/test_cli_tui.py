from __future__ import annotations

from pathlib import Path

from syndcrawler.cli import _read_env_file, build_parser


def test_cli_exposes_tui_command() -> None:
    args = build_parser().parse_args(
        [
            "tui",
            "--api-url",
            "http://127.0.0.1:8082",
            "--env-file",
            "/tmp/crawler.env",
            "--binary",
            "/tmp/syndcrawler-tui",
        ]
    )
    assert args.command == "tui"
    assert args.api_url == "http://127.0.0.1:8082"
    assert args.env_file == "/tmp/crawler.env"
    assert args.binary == "/tmp/syndcrawler-tui"


def test_tui_env_file_parser_handles_comments_exports_and_quotes(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
        # deployment configuration
        export SYNCRAWLER_API_TOKEN=secret
        SYNCRAWLER_API_PORT=8082
        SYNCRAWLER_API_BIND="127.0.0.1"
        EMPTY=
        malformed-line
        """,
        encoding="utf-8",
    )

    assert _read_env_file(env_file) == {
        "SYNCRAWLER_API_TOKEN": "secret",
        "SYNCRAWLER_API_PORT": "8082",
        "SYNCRAWLER_API_BIND": "127.0.0.1",
        "EMPTY": "",
    }
