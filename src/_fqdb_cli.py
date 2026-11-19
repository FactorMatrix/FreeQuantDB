"""Installed console launcher. The CLI chooses its root after parsing arguments."""

import sys


def main():
    sys._fqdb_console_import = True
    try:
        from fqdb.cli import main as run
    finally:
        del sys._fqdb_console_import
    run()
