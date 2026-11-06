"""Entry point for the standalone executable."""

import os
import sys

from callmap.cli import main

if __name__ == "__main__":
    # MCP's UTF-8 stream wrapper may close stdout during shutdown.
    # The frozen runtime flushes sys.stdout after main returns.
    saved_stdout = os.dup(sys.stdout.fileno())
    try:
        main()
    finally:
        # Wrapper finalizers may run after this block, so restore an independent
        # descriptor even when the original stream is still open here.
        if not sys.stdout.closed:
            sys.stdout.flush()
        sys.stdout = os.fdopen(saved_stdout, "w", encoding="utf-8")
        sys.__stdout__ = sys.stdout
