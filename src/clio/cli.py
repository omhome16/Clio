# src/clio/cli.py
"""Headless CLI demo: analyze a repo with visible event stream."""
import argparse
import asyncio
import json

from clio.config import Limits, get_limits
from clio.events import Event, EventBus, SseFormatter
from clio.llm import LLMMessage, MockLLM
from clio.orchestrator import Orchestrator
from clio.sandbox import Sandbox


def _mock_handler(limits: Limits):
    def handler(messages: list[LLMMessage], model: str | None) -> str:
        if model == limits.frontier_model:
            return json.dumps({"final": '{"summary": "merged", "modules": ["core"]}'})
        if len(messages) < 3:
            return json.dumps({"tool": "list_tree", "args": {}})
        return json.dumps({"final": '{"findings": ["mock finding"]}'})
    return handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clio", description="Analyze a git repository")
    parser.add_argument("url", help="https://github.com/... or file:// repo URL")
    parser.add_argument(
        "--provider", choices=["mock", "gemini"], default="mock",
        help="LLM provider (default: mock, no API key needed)",
    )
    parser.add_argument("--job-id", default=None, help="override the generated job id")
    return parser


async def amain(args: argparse.Namespace) -> int:
    limits = get_limits()
    bus = EventBus()
    bus.subscribe(lambda e: print(f"[{e.ts[11:19]}] {e.type} {json.dumps(e.data)[:160]}"))
    sandbox = Sandbox(root=limits.workspace_root, limits=limits)
    if args.provider == "gemini":
        from clio.llm import GeminiClient
        client = GeminiClient()
    else:
        client = MockLLM(handler=_mock_handler(limits))
    orchestrator = Orchestrator(sandbox, client, bus=bus, limits=limits)
    report = await orchestrator.run(args.url, root=sandbox.root, job_id=args.job_id)
    print("REPORT:")
    print(json.dumps(report.to_dict(), indent=2))
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(amain(build_parser().parse_args())))


if __name__ == "__main__":
    main()
