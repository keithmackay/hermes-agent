"""Snugban dispatches run for real: a gateway webhook route with a ``snugban`` block, durable admission
and execution, the real terminal tool, a loopback model that scripts the agent's tool calls, and a fake
Snugban REST server. Checks what route tests cannot: the run works in its dispatch's own checkout, abort
stops a run blocked in a tool, Keith's answer is steered into a running tool loop, and a run that ends
without reporting fails its dispatch. Runs in a subprocess with its own HERMES_HOME, like
``test_webhook_authority``."""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SECRET = "snugban-runs-secret"
AGENT_KEY = "sb_agent_runs_key"


def test_dispatch_runs_in_its_checkout_stop_and_steer_reach_the_real_run(tmp_path):
    result = subprocess.run([sys.executable, __file__], env=dict(os.environ, HERMES_HOME=str(tmp_path)),
                            capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((tmp_path / "snugban-runs.json").read_text())
    assert summary == {"workspace": "checkout", "aborted_in_seconds_below": 20, "steered": True,
                       "failed_open_dispatch": True, "workspaces_removed": True}


def _dispatch(dispatch_id, task_id, script, delivery):
    return {"type": "dispatch", "dispatchId": dispatch_id, "deliveryId": delivery,
            "task": {"id": task_id, "title": f"Scripted {script}", "description": f"SCRIPT_{script}",
                     "priority": "P2", "dodCriteria": "none"},
            "project": {"slug": "demo", "name": "Demo", "repo": {"githubRepo": "keithmackay/demo"},
                        "defaultBranch": "main"},
            "branch": f"snugban/{script.lower()}", "instructions": "Scripted test run.",
            "return": {"ack": "", "progress": "", "heartbeat": "", "complete": "", "fail": "", "ask": "",
                       "heartbeatEverySeconds": 3600}}


def _signed(payload):
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    digest = hmac.new(SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return body, {"Content-Type": "application/json", "X-Snugban-Delivery": payload["deliveryId"],
                  "X-Snugban-Timestamp": timestamp, "X-Snugban-Signature": f"sha256={digest}"}


def _make_remote(bare):
    """A bare repository standing in for GitHub, with one commit on main."""
    def git(*args, cwd):
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args], cwd=cwd,
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)
    bare.parent.mkdir(parents=True)
    git("init", "--quiet", "--bare", "--initial-branch=main", str(bare), cwd=bare.parent)
    work = bare.parent / "work"
    git("clone", "--quiet", str(bare), str(work), cwd=bare.parent)
    (work / "README.md").write_text("demo\n")
    git("add", "README.md", cwd=work)
    git("commit", "--quiet", "-m", "start", cwd=work)
    git("push", "--quiet", "origin", "HEAD:main", cwd=work)


async def probe(model, root):
    import aiohttp
    from aiohttp import web
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from tests.waiting import async_wait_until

    reports = []

    async def _report(request):
        reports.append((request.match_info["step"], request.match_info["id"], await request.read()))
        return web.json_response({"dispatchId": request.match_info["id"], "state": "running"})

    snugban = web.Application()
    snugban.router.add_post("/api/peer/dispatches/{id}/{step}", _report)
    snugban_runner = web.AppRunner(snugban)
    await snugban_runner.setup()
    site = web.TCPSite(snugban_runner, "127.0.0.1", 0)
    await site.start()
    snugban_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    runner = GatewayRunner()
    await initialize_session_authority(runner, profile_id="default", instance_id="snugban-runs")
    checkouts, remotes = root / "checkouts", root / "remotes"
    _make_remote(remotes / "keithmackay" / "demo.git")
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1", "port": 0, "routes": {"snugban": {
            "secret": SECRET, "toolsets": ["terminal"],
            "snugban": {"base_url": snugban_url, "agent_key": AGENT_KEY, "workspace_root": str(checkouts),
                        "git_remote_base": f"file://{remotes}/"}}}}))
    adapter.gateway_runner = runner
    runner.adapters[Platform.WEBHOOK] = adapter
    runner._wire_adapter_handlers(adapter)
    assert await adapter.connect()
    port = next(iter(adapter._runner.sites))._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/webhooks/snugban"
    summary = {}
    try:
        async with aiohttp.ClientSession() as client:
            async def post(payload):
                body, headers = _signed(payload)
                async with client.post(url, data=body, headers=headers) as resp:
                    return resp.status

            # 1. The run's terminal starts in its own checkout; ending without a report fails the dispatch.
            assert await post(_dispatch("01PWDDISPATCH", "01PWDTASK", "PWD", "01DELIVERYPWD")) == 202
            await async_wait_until(lambda: any(step == "fail" and ident == "01PWDDISPATCH"
                                               for step, ident, _ in reports), timeout=60, what="the fail report")
            pwd_output = model.tool_output("SCRIPT_PWD")
            assert str(checkouts / "01PWDDISPATCH") in pwd_output, pwd_output
            summary["workspace"] = "checkout"
            summary["failed_open_dispatch"] = True

            # 2. Abort stops a run blocked in a long tool call, well before the call would end.
            assert await post(_dispatch("01SLEEPDISPATCH", "01SLEEPTASK", "SLEEP", "01DELIVERYSLEEP")) == 202
            started = checkouts / "01SLEEPDISPATCH" / "started"
            await async_wait_until(started.exists, timeout=60, what="the run's long tool call to start")
            began = time.monotonic()
            assert await post({"type": "abort", "dispatchId": "01SLEEPDISPATCH",
                               "deliveryId": "01DELIVERYABORT"}) == 200
            await async_wait_until(lambda: not (checkouts / "01SLEEPDISPATCH").exists(), timeout=40,
                                   what="the aborted run to end and its checkout to go")
            assert time.monotonic() - began < 20
            assert not any(step == "fail" and ident == "01SLEEPDISPATCH" for step, ident, _ in reports)
            summary["aborted_in_seconds_below"] = 20

            # 3. Keith's answer reaches the running tool loop of its task.
            assert await post(_dispatch("01STEERDISPATCH", "01STEERTASK", "STEER", "01DELIVERYSTEER")) == 202
            started = checkouts / "01STEERDISPATCH" / "started"
            await async_wait_until(started.exists, timeout=60, what="the run's tool call to start")
            assert await post({"type": "decision", "questionId": "01QSTEER", "taskId": "01STEERTASK",
                               "answer": "ANSWER_FROM_KEITH", "deliveryId": "01DELIVERYDECISION"}) == 200
            await async_wait_until(lambda: model.saw_after_tool("SCRIPT_STEER", "ANSWER_FROM_KEITH"), timeout=60,
                                   what="the answer in the run's next model request")
            summary["steered"] = True
            await async_wait_until(lambda: not any(checkouts.iterdir()), timeout=60,
                                   what="every checkout to be removed")
            summary["workspaces_removed"] = True
    finally:
        await adapter.disconnect()
        await snugban_runner.cleanup()
    Path(os.environ["HERMES_HOME"], "snugban-runs.json").write_text(json.dumps(summary))


def main():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    commands = {"SCRIPT_PWD": "pwd", "SCRIPT_SLEEP": "touch started && sleep 60",
                "SCRIPT_STEER": "touch started && sleep 4"}

    class ScriptedModel(BaseHTTPRequestHandler):
        """Calls the terminal once with its script's command, then answers in text."""

        def log_message(self, *args):
            pass

        _streaming = False

        def do_GET(self):
            self._send({"data": [{"id": "local-wire-stub", "object": "model"}]})

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self._streaming = bool(request.get("stream"))
            messages = request.get("messages") or []
            script = next((name for name in commands if name in json.dumps(messages)), None)
            if script is None:  # side tasks (titles, summaries)
                self._send(self._reply({"role": "assistant", "content": "ok"}, "stop"))
                return
            self.server.requests.append((script, messages))
            if any(m.get("role") == "tool" for m in messages):
                self._send(self._reply({"role": "assistant", "content": "Done; not reporting."}, "stop"))
                return
            call = {"id": "call_1", "type": "function", "function": {
                "name": "terminal", "arguments": json.dumps({"command": commands[script], "timeout": 90})}}
            self._send(self._reply({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"))

        @staticmethod
        def _reply(message, finish):
            return {"id": "chatcmpl-local", "model": "local-wire-stub", "created": 1, "object": "chat.completion",
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

        def _send(self, payload):
            self.send_response(200)
            streaming = self._streaming and "choices" in payload
            self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
            self.end_headers()
            if not streaming:
                self.wfile.write(json.dumps(payload).encode())
                return
            choice = payload["choices"][0]
            message = {**choice["message"]}
            if message.get("tool_calls"):
                message["tool_calls"] = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
            base = {key: payload[key] for key in ("id", "model", "created")}
            for delta, finish in ((message, None), ({}, choice["finish_reason"])):
                chunk = {**base, "object": "chat.completion.chunk",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

    model = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedModel)
    model.requests = []

    def tool_output(script):
        return "\n".join(json.dumps(m.get("content")) for name, messages in model.requests if name == script
                         for m in messages if m.get("role") == "tool")

    def saw_after_tool(script, text):
        return any(text in json.dumps(messages) for name, messages in model.requests
                   if name == script and any(m.get("role") == "tool" for m in messages))

    model.tool_output, model.saw_after_tool = tool_output, saw_after_tool
    threading.Thread(target=model.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{model.server_port}/v1"
    os.environ.update(OPENAI_API_KEY="loopback-only", OPENAI_BASE_URL=url)
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        f"model:\n  default: local-wire-stub\n  provider: custom\n  base_url: {url}\n"
        "streaming:\n  enabled: false\nauxiliary:\n  title_generation:\n    enabled: false\n")
    try:
        asyncio.run(probe(model, home))
    finally:
        model.shutdown()
        model.server_close()


if __name__ == "__main__":
    import traceback
    status = 0
    try:
        main()
    except BaseException:
        traceback.print_exc()
        status = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(status)
