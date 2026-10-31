"""Command line entry point: `python -m emporio <command>`."""
from __future__ import annotations

import argparse
import sys

from .config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m emporio",
        description="Empório da Música customer service agent.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    chat = sub.add_parser("chat", help="talk to the agent in the terminal")
    chat.add_argument("--session", help="conversation id to resume (e.g. the customer's phone)")
    chat.add_argument("--debug", action="store_true", help="show tool calls and latency")

    sub.add_parser("handoffs", help="list the open handoffs to the human team")
    sub.add_parser("data-report", help="print the data quality issues found and how they are handled")
    evaluate = sub.add_parser("eval", help="run the evaluation cases against the configured model")
    evaluate.add_argument("--only", help="run a single case by id")
    sub.add_parser("examples", help="record the example conversations into examples/")

    args = parser.parse_args(argv)
    settings = load_settings()

    if args.command == "data-report":
        from .catalog import Store
        from .quality import print_report

        print_report(Store.load(settings.data_dir), settings.now().date())
        return 0

    if args.command == "handoffs":
        from .cli import print_handoffs

        print_handoffs(settings)
        return 0

    if args.command == "eval":
        from .evals import run_eval

        return run_eval(settings, only=args.only)

    if args.command == "chat":
        from .cli import run_chat

        run_chat(settings, args.session, args.debug)
        return 0

    print(f"'{args.command}' is not implemented yet (model: {settings.llm_model}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
