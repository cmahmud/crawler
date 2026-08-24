from syndcrawler.cli import build_parser


def test_server_cli_defaults() -> None:
    args = build_parser().parse_args(["serve"])
    assert args.command == "serve"
    assert args.host == "127.0.0.1"
    assert args.port == 8080
    assert args.log_level == "info"


def test_worker_cli_accepts_distributed_recrawl_settings() -> None:
    args = build_parser().parse_args(
        [
            "worker",
            "--once",
            "--max-crawls",
            "12",
            "--per-crawl-limit",
            "4",
            "--recrawl-limit",
            "50",
            "--recrawl-lease-seconds",
            "90",
        ]
    )
    assert args.command == "worker"
    assert args.once is True
    assert args.max_crawls == 12
    assert args.per_crawl_limit == 4
    assert args.recrawl_limit == 50
    assert args.recrawl_lease_seconds == 90.0
