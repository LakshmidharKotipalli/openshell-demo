"""The ONLY module that talks to OpenShell.

Why a CLI wrapper rather than the Python SDK: the SDK (`openshell` on PyPI) is workspace-scoped
gRPC with no policy-set-with-wait or log-follow helper. The CLI exposes every operation this app
needs (verified against v0.1.7 `--help`, see docs/cli-help/), and its behaviour is what an
operator sees. Everything funnels through `_run` / `exec_stream`, which add timeouts and scrub
secrets from anything that could be logged or sent to the browser.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass
from typing import AsyncIterator, Sequence

from . import parsing

OPENSHELL_BIN = os.environ.get("OPENSHELL_BIN", "openshell")
DEMO_LABEL = "app=osc-demo"
NAME_PREFIX = "osc-demo-"
STREAM_LINE_LIMIT = 1024 * 1024  # max length of one exec output line (asyncio's default is 64 KiB)


class OpenShellError(RuntimeError):
    def __init__(self, message: str, returncode: int | None = None, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str
    seconds: float


def _secrets_from_env() -> list[str]:
    vals = []
    for k in ("OPENROUTER_API_KEY",):
        v = os.environ.get(k)
        if v:
            vals.append(v)
    return vals


class OpenShellClient:
    def __init__(self, binary: str = OPENSHELL_BIN, gateway: str | None = None):
        self.binary = binary
        self.gateway = gateway
        self._scrub = parsing.SecretScrubber(_secrets_from_env())

    # -- plumbing ---------------------------------------------------------
    def _argv(self, args: Sequence[str]) -> list[str]:
        argv = [self.binary, "--color", "never"]
        if self.gateway:
            argv += ["--gateway", self.gateway]
        return argv + list(args)

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    async def _run(self, args: Sequence[str], *, timeout: float = 60, check: bool = True) -> CmdResult:
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv(args),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise OpenShellError(f"openshell CLI not found ({self.binary})") from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise OpenShellError(f"`openshell {' '.join(args[:2])}` timed out after {timeout:.0f}s", returncode=124)
        res = CmdResult(
            proc.returncode if proc.returncode is not None else -1,
            self._scrub.scrub(out.decode("utf-8", "replace")),
            self._scrub.scrub(err.decode("utf-8", "replace")),
            time.monotonic() - t0,
        )
        if check and res.returncode != 0:
            msg = (res.stderr or res.stdout).strip().splitlines()
            raise OpenShellError(
                f"`openshell {' '.join(args[:2])}` failed (exit {res.returncode}): {' '.join(msg[-4:])[:500]}",
                returncode=res.returncode,
                stderr=res.stderr,
            )
        return res

    # -- gateway / health -------------------------------------------------
    async def status(self) -> str:
        return (await self._run(["status"], timeout=15)).stdout

    async def is_connected(self) -> bool:
        try:
            return "Connected" in await self.status()
        except OpenShellError:
            return False

    # -- providers / profiles --------------------------------------------
    async def provider_names(self) -> list[str]:
        res = await self._run(["provider", "list", "--names"], timeout=20)
        return [l.strip() for l in res.stdout.splitlines() if l.strip()]

    async def profile_lint(self, path: str) -> None:
        await self._run(["profile", "lint", "-f", path], timeout=30)

    async def profile_import(self, path: str) -> None:
        await self._run(["profile", "import", "-f", path], timeout=30)

    # -- sandboxes --------------------------------------------------------
    async def list_sandboxes(self) -> list[dict]:
        res = await self._run(["sandbox", "list", "-o", "json"], timeout=20)
        text = res.stdout.strip()
        if not text:
            return []
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("sandboxes", [])
        return data

    async def list_demo_sandbox_names(self) -> list[str]:
        names = []
        for sb in await self.list_sandboxes():
            name = sb.get("name") or sb.get("metadata", {}).get("name") or ""
            if name.startswith(NAME_PREFIX):
                names.append(name)
        return names

    async def create_sandbox(
        self,
        name: str,
        *,
        image: str,
        policy_path: str,
        providers: Sequence[str] = (),
        timeout: float = 240,
    ) -> CmdResult:
        args = [
            "sandbox", "create", "--name", name, "--from", image,
            "--policy", policy_path, "--label", DEMO_LABEL, "--detach", "--no-tty",
        ]
        for pr in providers:
            args += ["--provider", pr]
        args += ["--", "sleep", "infinity"]
        return await self._run(args, timeout=timeout)

    async def delete_sandbox(self, *names: str, timeout: float = 90) -> None:
        if names:
            await self._run(["sandbox", "delete", *names], timeout=timeout, check=False)

    async def upload(self, name: str, local: str, dest: str | None = None, timeout: float = 60) -> None:
        args = ["sandbox", "upload", name, local]
        if dest:
            args.append(dest)
        args.append("--no-git-ignore")
        await self._run(args, timeout=timeout)

    async def exec_stream(
        self, name: str, argv: Sequence[str], *, timeout: float = 180, env: dict[str, str] | None = None
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield ("stdout"|"stderr", line) as the command runs, then ("exit", "<code>")."""
        args = ["sandbox", "exec", "-n", name, "--no-tty", "--timeout", str(int(timeout))]
        for k, v in (env or {}).items():
            args += ["--env", f"{k}={v}"]
        args += ["--", *argv]
        proc = await asyncio.create_subprocess_exec(
            *self._argv(args),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LINE_LIMIT,
        )
        q: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

        async def pump(stream: asyncio.StreamReader, tag: str) -> None:
            try:
                while True:
                    try:
                        raw = await stream.readline()
                    except ValueError:  # a line longer than STREAM_LINE_LIMIT: drop it, keep streaming
                        continue
                    if not raw:
                        break
                    await q.put((tag, self._scrub.scrub(raw.decode("utf-8", "replace").rstrip("\n"))))
            finally:
                await q.put(None)  # always signal end-of-stream, or the consumer waits for the full timeout

        tasks = [asyncio.create_task(pump(proc.stdout, "stdout")), asyncio.create_task(pump(proc.stderr, "stderr"))]
        done = 0
        deadline = time.monotonic() + timeout + 10 if timeout > 0 else float("inf")
        try:
            while done < 2:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                item = await asyncio.wait_for(q.get(), timeout=remaining)
                if item is None:
                    done += 1
                    continue
                yield item
            rc = await asyncio.wait_for(proc.wait(), timeout=15)
            yield ("exit", str(rc))
        except asyncio.TimeoutError:
            proc.kill()
            yield ("exit", "124")
        finally:
            if proc.returncode is None:
                proc.kill()
            for t in tasks:
                t.cancel()

    async def exec_capture(self, name: str, argv: Sequence[str], *, timeout: float = 60) -> CmdResult:
        args = ["sandbox", "exec", "-n", name, "--no-tty", "--timeout", str(int(timeout)), "--", *argv]
        return await self._run(args, timeout=timeout + 10, check=False)

    # -- logs / denials ---------------------------------------------------
    async def sandbox_logs(self, name: str, *, n: int = 300, since: str | None = None) -> list[str]:
        args = ["logs", name, "--source", "sandbox", "-n", str(n)]
        if since:
            args += ["--since", since]
        res = await self._run(args, timeout=30)
        return res.stdout.splitlines()

    async def denials(self, name: str, *, since: str | None = None) -> list[parsing.Denial]:
        return parsing.parse_denials(await self.sandbox_logs(name, since=since))

    # -- policy -----------------------------------------------------------
    async def policy_base(self, name: str) -> tuple[dict, dict]:
        res = await self._run(["policy", "get", name, "--base", "-o", "json"], timeout=30)
        return parsing.parse_policy_get_json(res.stdout)

    async def policy_effective_yaml(self, name: str) -> str:
        """Effective policy (base + provider-composed rules) as YAML text for display."""
        res = await self._run(["policy", "get", name, "--full", "-o", "json"], timeout=30)
        policy, _ = parsing.parse_policy_get_json(res.stdout)
        return parsing.dump_yaml(policy)

    async def policy_set(self, name: str, path: str, *, wait: bool = True, timeout: int = 60) -> float:
        """Apply a full policy file. Returns elapsed seconds (the 'apply took X s' figure)."""
        args = ["policy", "set", name, "--policy", path]
        if wait:
            args += ["--wait", "--timeout", str(timeout)]
        res = await self._run(args, timeout=timeout + 15, check=False)
        if res.returncode == 124:
            raise OpenShellError("policy apply timed out waiting for the sandbox to load it (it may still load)", 124, res.stderr)
        if res.returncode != 0:
            msg = (res.stderr or res.stdout).strip().splitlines()
            raise OpenShellError(f"policy apply rejected: {' '.join(msg[-4:])[:500]}", res.returncode, res.stderr)
        return res.seconds
