"""Build a native single-file CLI with MCP, Web and Jedi support."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "callmap",
        "--distpath",
        str(root / "dist"),
        "--workpath",
        str(root / "build" / "pyinstaller"),
        "--specpath",
        str(root / "build"),
        "--paths",
        str(root),
        "--collect-submodules",
        "mcp.server",
        "--collect-data",
        "mcp",
        "--collect-all",
        "jedi",
        "--collect-all",
        "parso",
        "--recursive-copy-metadata",
        "mcp",
        "--copy-metadata",
        "jedi",
        "--copy-metadata",
        "parso",
        "--add-data",
        f"{root / 'callmap' / 'web'}:callmap/web",
    ]
    # GraphStore fingerprints engine sources to invalidate stale indexes.
    # Bundle the exact sources compiled into this executable.
    for source in sorted((root / "callmap").glob("*.py")):
        command += ["--add-data", f"{source}:callmap"]
    command.append(str(root / "scripts" / "binary_entry.py"))
    subprocess.run(command, cwd=root, check=True)


if __name__ == "__main__":
    main()
