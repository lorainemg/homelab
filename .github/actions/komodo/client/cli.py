#!/usr/bin/env python3
"""Command line over komodo.py, used by the composite actions beside it.

Standard library only, like the client it wraps: this runs on a GitHub runner
and on a freshly installed homelab server, with nothing to pip install in
either place.
"""
from __future__ import annotations

import argparse
import functools
import os
import sys
from pathlib import Path

from komodo import Komodo, KomodoError, expand, load_vars


_REQUIRED_ENV = ("KOMODO_URL", "KOMODO_API_KEY", "KOMODO_API_SECRET")


class Cli:
    """Owns the command line: builds the parser, holds the parsed arguments,
    and lazily builds the client that the networked commands need."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args

    @functools.cached_property
    def client(self) -> Komodo:
        """Built on first use, so a command that never reads it never needs
        credentials. That is what keeps `render` working on a fresh server."""
        missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
        if missing:
            raise KomodoError(f"missing environment: {', '.join(missing)}")
        return Komodo(
            url=os.environ["KOMODO_URL"],
            api_key=os.environ["KOMODO_API_KEY"],
            api_secret=os.environ["KOMODO_API_SECRET"],
            poll_interval=int(os.environ.get("KOMODO_POLL_INTERVAL", "5")),
        )

    def deploy_stack(self) -> None:
        args = self.args
        accepted = self.client.call("execute", "DeployStack", {"stack": args.stack})
        self.client.await_update(accepted["_id"]["$oid"], timeout=args.timeout)

    def update_stack(self) -> None:
        args = self.args
        if args.create_if_missing and not self.client.stack_exists(args.stack):
            print(f"stack {args.stack} does not exist yet; creating it")
            self.client.call("write", "CreateStack", {
                "name": args.stack,
                "config": {
                    "server_id": args.server,
                    "project_name": args.stack,
                    "file_contents": "services: {}",
                    "webhook_enabled": False,
                },
            })
        config = self.client.stack_config(
            args.compose_file, args.env_file, args.links, load_vars())
        self.client.call("write", "UpdateStack", {"id": args.stack, "config": config})
        print(f"stack {args.stack} updated")

    def run_sync(self) -> None:
        args = self.args
        config = self.client.sync_config(args.contents_file, load_vars())
        self.client.call(
            "write", "UpdateResourceSync", {"id": args.sync, "config": config})
        accepted = self.client.call("execute", "RunSync", {"sync": args.sync})
        self.client.await_update(accepted["_id"]["$oid"], timeout=args.timeout)

    def render(self) -> None:
        # Never reads self.client, so no credentials are ever required: this
        # exists for scripts/bootstrap.sh, which renders the same file on a
        # fresh server where no API key exists yet.
        print(expand(Path(self.args.file).read_text(), load_vars()), end="")

    @staticmethod
    def build_parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description=__doc__)
        sub = parser.add_subparsers(dest="command", required=True)

        deploy = sub.add_parser("deploy-stack", help="deploy a stack and wait")
        deploy.add_argument("--stack", required=True)
        deploy.add_argument("--timeout", type=int, default=300)
        deploy.set_defaults(handler=Cli.deploy_stack)

        update = sub.add_parser("update-stack", help="push a stack's definition")
        update.add_argument("--stack", required=True)
        update.add_argument("--compose-file", default=None)
        update.add_argument("--env-file", default=None)
        update.add_argument("--links", default=None)
        update.add_argument("--create-if-missing", action="store_true")
        update.add_argument("--server", default="Local")
        update.set_defaults(handler=Cli.update_stack)

        sync = sub.add_parser("run-sync", help="push a sync's contents and run it")
        sync.add_argument("--sync", required=True)
        sync.add_argument("--contents-file", required=True)
        sync.add_argument("--timeout", type=int, default=300)
        sync.set_defaults(handler=Cli.run_sync)

        render = sub.add_parser("render", help="expand a file's ${VARS} and print it")
        render.add_argument("file")
        render.set_defaults(handler=Cli.render)

        return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the composite actions. Never raises: an exception
    becomes exit 1 with a readable message, which is what fails the step."""
    args = Cli.build_parser().parse_args(argv)
    try:
        args.handler(Cli(args))
    except KomodoError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
