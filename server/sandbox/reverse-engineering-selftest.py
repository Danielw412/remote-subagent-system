#!/usr/bin/env python3
"""Offline smoke test of the configured REA MCP server and Ghidra provider."""
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time


def verify():
    # Ask the installed OpenCode to parse the additive config, including the
    # instruction path and MCP schema. No model or public network is needed.
    sources = json.loads(subprocess.check_output(
        ["opencode", "debug", "config"], text=True, timeout=30))
    config = next(s["info"] for s in sources
                  if s.get("path") == "/etc/rsa/reverse-engineering.json")
    command = config["mcp"]["servers"]["rea"]["command"]
    for path in config["instructions"]:
        if not Path(path).is_file():
            raise RuntimeError("missing agent instructions: " + path)
    skill = Path("/root/.config/opencode/skills/reverse-engineer-anything")
    if not (skill / "SKILL.md").is_file() or not (skill / "references").is_dir():
        raise RuntimeError("REA skill or references missing")

    with tempfile.TemporaryDirectory(prefix="rsa-re-selftest-") as td:
        scratch = Path(td)
        source, binary = scratch / "fixture.c", scratch / "fixture"
        source.write_text("int rsa_fixture(void) { return 1729; }\n"
                          "int main(void) { return rsa_fixture(); }\n")
        subprocess.run(["cc", "-O0", "-g", "-o", str(binary), str(source)],
                       check=True, timeout=30)
        # Never execute the fixture. Ghidra inspects its bytes statically.
        with (scratch / "stderr.log").open("w+") as stderr:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=stderr,
                                    text=True, start_new_session=True, cwd=td)
            messages = queue.Queue()

            def read():
                for line in proc.stdout:
                    messages.put(line)
                messages.put(None)

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            sequence = 0

            def send(message):
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
                proc.stdin.flush()

            def request(method, params):
                nonlocal sequence
                sequence += 1
                send({"id": sequence, "method": method, "params": params})
                deadline = time.monotonic() + 240
                while True:
                    line = messages.get(timeout=max(0, deadline - time.monotonic()))
                    if line is None:
                        raise RuntimeError("REA exited before replying")
                    reply = json.loads(line)
                    if reply.get("id") != sequence:
                        continue
                    if "error" in reply:
                        raise RuntimeError(str(reply["error"]))
                    result = reply["result"]
                    if result.get("isError"):
                        raise RuntimeError(json.dumps(result)[:2000])
                    return result

            def call(name, arguments):
                return request("tools/call", {"name": name, "arguments": arguments})

            try:
                request("initialize", {"protocolVersion": "2024-11-05",
                                       "capabilities": {}, "clientInfo": {
                                           "name": "rsa-selftest", "version": "1"}})
                send({"method": "notifications/initialized"})
                tools = request("tools/list", {})
                names = {tool["name"] for tool in tools["tools"]}
                required = {"open_binary", "analyze_function", "close_binary",
                            "analyze_javascript_application"}
                if not required <= names:
                    raise RuntimeError("REA MCP tool catalog missing required tools")
                call("open_binary", {"path": str(binary), "provider_id": "ghidra"})
                analysis = call("analyze_function", {"procedure": "rsa_fixture"})
                rendered = json.dumps(analysis)
                if ("pseudocode" not in rendered or
                        not re.search(r"return\s+(?:1729|0x0*6c1)\b", rendered)):
                    raise RuntimeError("fixture pseudocode missing: " + rendered[:2000])
                call("close_binary", {})
                app = scratch / "app"
                app.mkdir()
                (app / "package.json").write_text('{"name":"rsa-fixture","main":"index.js"}')
                (app / "index.js").write_text("exports.rsa_answer = () => 1729;\n")
                evidence = call("analyze_javascript_application", {"input_path": str(app)})
                if "rsa_answer" not in json.dumps(evidence):
                    raise RuntimeError("JavaScript fixture evidence missing")
                print("PASS configured REA MCP, native Ghidra pseudocode, JavaScript evidence")
            except Exception:
                stderr.flush()
                stderr.seek(0)
                print(stderr.read()[-3000:], file=sys.stderr)
                raise
            finally:
                # Terminate the owned process family even when a provider fails.
                proc.stdin.close()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=5)
                proc.stdout.close()
                reader.join(timeout=5)


if __name__ == "__main__":
    verify()
