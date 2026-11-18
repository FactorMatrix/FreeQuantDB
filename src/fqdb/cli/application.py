"""Argument parsing, data-root selection, and command execution."""

from __future__ import annotations

import argparse
import json
import sys

from ..config import select_root
from ..service.client import Client
from . import catalogs, local, sharing, transfers


def _data_dir_argument(parser):
    parser.add_argument(
        "-d",
        "--data-dir",
        metavar="DIRECTORY",
        help="Use this data root for this command; init saves the choice (otherwise configured root or ~/.fqdb)",
    )


class CommandLine:
    def __init__(self):
        self.root_options = argparse.ArgumentParser(prog="fqdb", add_help=False, allow_abbrev=False)
        _data_dir_argument(self.root_options)
        self.parser = argparse.ArgumentParser(
            prog="fqdb",
            description="Manage local data from the command line, web UI or Python.",
            epilog="Start with: python -m fqdb gateway. Use COMMAND -h to see its options.",
            parents=[self.root_options],
            allow_abbrev=False,
        )
        self.commands = self.parser.add_subparsers(dest="command", required=True)

    def add(self, name, description, handler):
        command = self.commands.add_parser(
            name, help=description, description=description, parents=[self.root_options], allow_abbrev=False
        )
        command.set_defaults(handler=handler)
        return command

    def run(self, argv):
        # Parse the shared option separately so it also works after a subcommand.
        root, rest = self.root_options.parse_known_args(argv)
        args = vars(self.parser.parse_args(rest))
        handler = args.pop("handler")
        args.pop("command")
        args.pop("data_dir")
        try:
            client = Client(root.data_dir)
            select_root(client.config.root, persist=False)
            result = handler(client, args)
            if result is not None:
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        except Exception as exc:
            self.parser.exit(1, f"{self.parser.prog}: {exc}\n")


def main(argv=None):
    command_line = CommandLine()
    for group in (local, transfers, sharing, catalogs):
        group.register(command_line)
    command_line.run(sys.argv[1:] if argv is None else argv)
