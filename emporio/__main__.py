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
    evaluate.add_argument("--repeat", type=int, default=1, metavar="N",
                          help="run each case N times and report consistency (default 1)")
    sub.add_parser("examples", help="record the example conversations into examples/")
    serve = sub.add_parser("serve", help="start the web app and HTTP API")
    serve.add_argument("--host", default="127.0.0.1", help="interface to bind (0.0.0.0 in Docker)")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("feedback", help="list answers rated down in the web app (candidates for eval cases)")

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

        return run_eval(settings, only=args.only, repeat=max(1, args.repeat))

    if args.command == "examples":
        from .examples import record_examples

        record_examples(settings)
        return 0

    if args.command == "serve":
        import uvicorn

        from .web.api import create_app

        print(f"Empório da Música web app on http://{args.host}:{args.port}  (model: {settings.llm_model})")
        if args.host not in ("127.0.0.1", "localhost", "::1") and not settings.team_token:
            print("Warning: the team screens (tickets with customer contacts, metrics, knowledge base) are open to "
                  "anyone who can reach this port. Set TEAM_TOKEN to require an access code.")
        uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="warning")
        return 0

    if args.command == "feedback":
        from .cli import print_negative_feedback

        print_negative_feedback(settings)
        return 0

    if args.command == "chat":
        from .cli import run_chat

        run_chat(settings, args.session, args.debug)
        return 0

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
