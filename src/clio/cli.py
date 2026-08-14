# src/clio/cli.py
"""Headless CLI demo: analyze a repo with visible event stream."""
import argparse
import asyncio
import json
import logging

from clio.config import Limits, get_limits, get_provider
from clio.events import Event, EventBus, SseFormatter
from clio.impact import impact_of_symbol
from clio.llm import make_client
from clio.logging import setup_logging
from clio.reports import ReportArchive
from clio.orchestrator import Orchestrator
from clio.sandbox import Sandbox


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clio", description="Analyze a git repository")
    parser.add_argument("url", help="https://github.com/... or file:// repo URL")
    parser.add_argument(
        "--provider", choices=["gemini", "groq", "ollama"], default=get_provider(),
        help="LLM provider (default: $CLIO_PROVIDER, gemini if unset)",
    )
    parser.add_argument("--job-id", default=None, help="override the generated job id")
    parser.add_argument(
        "--impact", default=None,
        help="symbol id (module::name) to run impact analysis for; prints IMPACT instead of REPORT",
    )
    return parser


async def amain(args: argparse.Namespace) -> int:
    limits = get_limits()
    bus = EventBus()
    bus.subscribe(lambda e: print(f"[{e.ts[11:19]}] {e.type} {json.dumps(e.data)[:160]}"))
    sandbox = Sandbox(root=limits.workspace_root, limits=limits)
    client = make_client(args.provider, limits)
    orchestrator = Orchestrator(sandbox, client, bus=bus, limits=limits)
    report = await orchestrator.run(args.url, root=sandbox.root, job_id=args.job_id)
    if args.impact:
        archive = ReportArchive(sandbox.root)
        impact = impact_of_symbol(archive, report.job_id, args.impact)
        print("IMPACT:")
        print(json.dumps(impact.to_dict(), indent=2))
    else:
        print("REPORT:")
        print(json.dumps(report.to_dict(), indent=2))
    return 0


def main() -> int:
    setup_logging()
    try:
        return asyncio.run(amain(build_parser().parse_args()))
    except Exception:
        logging.getLogger("clio").exception("analysis failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
