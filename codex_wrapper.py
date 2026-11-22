#!/usr/bin/env python3
"""Run Codex from scripts with an optional model, reasoning effort, and profile."""

import argparse
import json
import subprocess
import sys


PROFILE_EXAMPLE = """\
To use an external provider, save a profile as
~/.codex/external.config.toml (or under your configured CODEX_HOME):

    model = "your-model-id"
    model_provider = "external"

    [model_providers.external]
    name = "External LLM"
    base_url = "https://your-gateway.example/v1"
    wire_api = "responses"
    env_key = "EXTERNAL_LLM_API_KEY"

Set EXTERNAL_LLM_API_KEY in the environment, then pass --profile external.
Use a gateway that supports the Responses API. The selected model determines
which reasoning effort levels are supported.
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=PROFILE_EXAMPLE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("prompt", nargs="?", help="prompt text, or - to read stdin")
    parser.add_argument("-m", "--model", help="override the configured model ID")
    parser.add_argument("-e", "--effort", help="reasoning effort, such as medium or high")
    parser.add_argument("-p", "--profile", help="select a named Codex configuration")
    parser.add_argument("-C", "--cd", metavar="DIR", help="working directory for Codex")
    parser.add_argument(
        "-s", "--sandbox", choices=("read-only", "workspace-write"),
        default="read-only", help="shell sandbox mode (default: read-only)",
    )
    parser.add_argument(
        "--skip-git-repo-check", action="store_true", help="allow runs outside a Git repo"
    )
    parser.add_argument("--json", action="store_true", help="emit Codex events as JSONL")
    search = parser.add_mutually_exclusive_group()
    search.add_argument("--web-search", action="store_true", help="enable live web search")
    search.add_argument("--no-web-search", action="store_true", help="disable Codex web search")
    parser.add_argument("--ephemeral", action="store_true", help="skip saving the session")
    parser.add_argument(
        "-o", "--output-last-message", metavar="FILE", help="save Codex's final reply"
    )
    args = parser.parse_args()

    if args.prompt is None or args.prompt == "-":
        if sys.stdin.isatty():
            parser.error("provide a prompt as an argument or pipe it through stdin")
        prompt = sys.stdin.buffer.read()
    else:
        prompt = args.prompt.encode("utf-8")
    if not prompt.strip():
        parser.error("the prompt is empty")

    command = [
        "codex", "exec", "--config", 'approval_policy="never"',
        "--sandbox", args.sandbox,
    ]
    if args.profile:
        command.extend(("--profile", args.profile))
    if args.model:
        command.extend(("--model", args.model))
    if args.effort:
        effort = json.dumps(args.effort, ensure_ascii=False)
        command.extend(("--config", f"model_reasoning_effort={effort}"))
    if args.cd:
        command.extend(("--cd", args.cd))
    if args.skip_git_repo_check:
        command.append("--skip-git-repo-check")
    if args.json:
        command.append("--json")
    if args.web_search:
        command.extend(("--config", 'web_search="live"'))
    if args.no_web_search:
        command.extend(("--config", 'web_search="disabled"'))
    if args.ephemeral:
        command.append("--ephemeral")
    if args.output_last_message:
        command.extend(("--output-last-message", args.output_last_message))

    # Pass the prompt through stdin so it cannot be parsed as a subcommand or flag.
    try:
        status = subprocess.run([*command, "-"], input=prompt, check=False).returncode
    except FileNotFoundError:
        print("codex_wrapper: 'codex' was not found on PATH", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"codex_wrapper: could not start Codex: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return status if status >= 0 else 128 - status


if __name__ == "__main__":
    sys.exit(main())
