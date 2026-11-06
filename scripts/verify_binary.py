"""Smoke-test the executable outside the checkout with no Python on PATH."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from urllib.request import urlopen


async def verify_mcp(binary: Path, project: Path, environment: dict) -> list[str]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(binary),
        args=["--cache-dir", str(project / "mcp-cache"), "mcp"],
        cwd=str(project),
        env=environment,
    )
    with tempfile.TemporaryFile(mode="w+") as errors:
        async with stdio_client(params, errlog=errors) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=60)
            ) as session:
                await session.initialize()
                tools = [tool.name for tool in (await session.list_tools()).tools]
                result = await session.call_tool(
                    "index_status", {"project_path": str(project)}
                )
                assert not result.isError, result.content
                stats = json.loads(result.content[0].text)
                assert not stats["errors"], stats
                result = await session.call_tool(
                    "read_symbol",
                    {"project_path": str(project), "symbol": "sample.main"},
                )
                assert not result.isError, result.content
                assert "def main" in json.loads(result.content[0].text)["code"]
        errors.seek(0)
        stderr = errors.read()
        assert "Traceback" not in stderr and "[PYI-" not in stderr, stderr
        return tools


def verify(binary: Path) -> dict:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX"}
    }
    environment["PATH"] = ""
    with tempfile.TemporaryDirectory(prefix="callmap-binary-") as directory:
        root = Path(directory)
        executable = root / binary.name
        shutil.copy2(binary, executable)
        project = root / "project"
        project.mkdir()
        (project / "sample.py").write_text(
            "import json\n\n"
            "def helper():\n    return 42\n\n"
            "def main():\n    return helper()\n\n"
            "def semantic():\n"
            "    decoder = json.JSONDecoder()\n"
            "    return decoder.decode('{}')\n\n"
            "if __name__ == '__main__':\n    main()\n"
        )

        def run(*args: str, project_args: bool = True) -> str:
            command = [str(executable)]
            if project_args:
                command += ["--project", str(project)]
            command += args
            result = subprocess.run(
                command,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=90,
            )
            if result.returncode:
                raise RuntimeError(
                    f"{command} exited {result.returncode}:\n{result.stderr}"
                )
            return result.stdout

        assert "Python" in run("--help", project_args=False)
        cold = json.loads(run("index"))
        warm = json.loads(run("index"))
        assert not cold["errors"], cold
        assert not cold["cache_hit"] and warm["cache_hit"], (cold, warm)
        assert json.loads(run("search", "main"))
        entries = json.loads(run("entries"))
        assert entries["auto_entry"], entries
        graph = json.loads(run("expand", "sample.main", "--depth", "2"))
        assert any(n["qualified_name"] == "sample.helper" for n in graph["nodes"])
        callers = json.loads(run("callers", "sample.helper"))
        assert any(n["qualified_name"] == "sample.main" for n in callers["nodes"])
        assert "def main" in json.loads(run("node", "sample.main"))["code"]
        semantic = json.loads(run("--jedi", "index"))
        assert not semantic["errors"], semantic
        inferred = json.loads(run("--jedi", "expand", "sample.semantic"))
        assert any(n["doc_source"] == "jedi-static" for n in inferred["nodes"]), (
            inferred
        )
        registration = json.loads(run("register", project_args=False))
        registered = registration["server_command"]
        assert Path(registered[0]).resolve() == executable.resolve(), registered
        assert registered[1:] == ["mcp"], registered
        run("index")  # Return to the default options before checking Web cache reuse.

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = subprocess.Popen(
            [str(executable), "--project", str(project), "serve", "--port", str(port)],
            cwd=root,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                        html = response.read().decode()
                    break
                except OSError:
                    if server.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("Web server did not start")
                    time.sleep(0.2)
            assert "<html" in html and "__CALLMAP_TOKEN__" not in html
            with urlopen(f"http://127.0.0.1:{port}/api/stats", timeout=10) as response:
                assert json.load(response)["cache_hit"]
        finally:
            server.terminate()
            server.communicate(timeout=10)
        tools = asyncio.run(verify_mcp(executable, project, environment))
        return {
            "binary": str(binary),
            "bytes": binary.stat().st_size,
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "isolated_working_directory": True,
            "python_on_path": False,
            "cli": "passed",
            "cache_reuse": "passed",
            "jedi": "passed",
            "registration_preview": "passed",
            "web": "passed",
            "mcp": "passed",
            "mcp_tools": tools,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = verify(args.binary.resolve())
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
