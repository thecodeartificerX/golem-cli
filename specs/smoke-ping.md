# Spec: Add `golem ping` CLI Command

## Goal
Add a minimal `ping` command to the Golem CLI that prints "pong" to stdout. This is a smoke test for the Golem pipeline.

## Requirements

1. Add a `ping` command to the Typer CLI in `src/golem/cli.py`
2. When invoked via `uv run golem ping`, it prints exactly: `pong`
3. No arguments, no options, no flags — just print and exit
4. Add a test in `tests/test_cli.py` that invokes the ping command and asserts the output contains "pong"

## Validation
```bash
uv run golem ping
```
Expected output: `pong`

```bash
uv run pytest tests/test_cli.py -k ping -x
```
Expected: test passes.

## Constraints
- Do not modify any existing commands or tests
- Do not add new files — only edit `src/golem/cli.py` and `tests/test_cli.py`
- Follow existing code style and patterns in both files
