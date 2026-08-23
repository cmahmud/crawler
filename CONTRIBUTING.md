# Contributing

1. Use Python 3.12 or newer.
2. Install development dependencies with `python -m pip install -e '.[dev]'`.
3. Run `ruff check .` and `pytest` before opening a pull request.
4. Keep fetch engines, network routes, frontier backends, parsers and extraction adapters behind interfaces; avoid coupling site-specific logic to the crawler kernel.
5. Do not add challenge bypass, CAPTCHA solving, credential abuse, stealth persistence, or target-scope expansion features.

Contributions are accepted under the repository's Apache-2.0 license.
