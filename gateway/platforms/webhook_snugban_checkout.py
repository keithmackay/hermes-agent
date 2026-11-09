"""The git checkout a Snugban dispatch works in: cloned by the receiver before the dispatch's run is
admitted, so where the work happens does not depend on the model.

A fresh dispatch's repository is cloned into a scratch folder beside the dispatch's folder, put on the
dispatch's branch (tracking it when the remote has it, else branched from the base branch), and only then
renamed into place, so a failure never touches the dispatch's folder. A folder that already holds a
checkout (a resend after a restart, while the run that works there may still be running) is reused as it
stands and no git runs in it: its ``.git/config`` is the run's to write, and git would run commands that
config names (``ext::`` remotes, credential helpers, ``uploadpack``). The run does its own fetching.

Git runs with the host's own git configuration, so credential helpers (``gh auth setup-git``, osxkeychain)
and SSH keys work. Overridden for these calls only: hooks (``core.hooksPath``) and fsmonitor are off,
terminal prompts (``GIT_TERMINAL_PROMPT=0``), askpass programs (``GIT_ASKPASS``, ``SSH_ASKPASS``,
``core.askPass``) and the Git Credential Manager's dialogs are off, and ssh runs with ``BatchMode=yes``.
All of it shares one deadline, and a timeout kills git's whole process group."""

import contextlib
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from hermes_cli._subprocess_compat import harden_git_argv

logger = logging.getLogger(__name__)

# The whole preparation (clone and branch) must end well inside Snugban's 8-minute window for an ack.
PREPARE_TIMEOUT_SECONDS = 300.0
_POST_KILL_DRAIN_SECONDS = 10.0
_GIT_CONFIG = ("-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false", "-c", "core.askPass=")
SCRATCH_PREFIXES = (".cloning-", ".removing-")
# Scratch names this module makes: .cloning-<dispatchId>-<8 hex> and .removing-<dispatchId>-<8 hex>.
_SCRATCH_NAME = re.compile(r"\.(cloning|removing)-[A-Za-z0-9_-]{1,100}-[0-9a-f]{8}")
# A half-made clone younger than this may belong to a clone still running (another gateway sharing the
# workspace root, or this one before a reconnect), so it is left alone.
_STALE_CLONE_SECONDS = 2 * PREPARE_TIMEOUT_SECONDS
_HEAD_REF = re.compile(r"ref: refs/heads/(\S+)")


@dataclass(frozen=True)
class Checkout:
    """What preparation found or made: whether this call made the checkout, and the branch it is on (None
    for a detached HEAD)."""
    made: bool
    branch: Optional[str]


class CheckoutError(Exception):
    """Why the checkout could not be made: short, and safe to send to Snugban (no output, no secrets)."""


def git_env(base: Mapping[str, str]) -> dict:
    """*base* (the served profile's child environment) set so git and ssh fail instead of prompting.

    ssh gets ``-o BatchMode=yes`` on top of whichever command the user chose, in git's own order: the
    ``GIT_SSH_COMMAND`` variable, else ``core.sshCommand`` from the user's global or system git config (never a
    checkout's), else plain ``ssh``. A ``GIT_SSH`` program (plink and the like may not take ``-o``) is left as
    it is; git runs in a session of its own, with no terminal to prompt on."""
    env = {key: value for key, value in base.items() if key not in ("GIT_ASKPASS", "SSH_ASKPASS")}
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", SSH_ASKPASS_REQUIRE="never")
    ssh = env.get("GIT_SSH_COMMAND", "").strip() or _configured_ssh_command(env)
    if not ssh and env.get("GIT_SSH", "").strip():
        return env
    ssh = ssh or "ssh"
    env["GIT_SSH_COMMAND"] = ssh if "BatchMode=yes" in ssh else f"{ssh} -o BatchMode=yes"
    return env


def _configured_ssh_command(env: dict) -> str:
    """``core.sshCommand`` from the user's global, else system, git config ('' when unset)."""
    for scope in ("--global", "--system"):
        result = subprocess.run(["git", "config", scope, "--get", "core.sshCommand"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=10, env=env,
                                stdin=subprocess.DEVNULL, check=False, cwd=os.path.expanduser("~"))
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return ""


def _group_popen_kwargs() -> dict:
    if os.name == "nt":
        from hermes_cli._subprocess_compat import windows_hide_flags
        return {"creationflags": windows_hide_flags() | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    # detached-spawn: ok — awaited git call; a timeout kills its whole group (_kill_group)
    return {"start_new_session": True}


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill git and every process it started (ssh, credential helpers, remote helpers)."""
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=10, check=False)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX only, the nt branch returned above


def _git(args: list, cwd: Path, env: dict, deadline: float, *, what: str, ok_codes: tuple = (0,)) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CheckoutError(f"{what} timed out")
    proc = subprocess.Popen(["git", *_GIT_CONFIG, *harden_git_argv(args)], cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", env=env, **_group_popen_kwargs())
    try:
        _stdout, stderr = proc.communicate(timeout=remaining)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=_POST_KILL_DRAIN_SECONDS)
        raise CheckoutError(f"{what} timed out") from None
    if proc.returncode not in ok_codes:
        logger.warning("[webhook] Snugban checkout: git %s failed (%s): %s", args[0], proc.returncode,
                       stderr.strip()[-500:])
        raise CheckoutError(f"{what} failed (git exit {proc.returncode})")
    return proc.returncode


def _existing_checkout(workspace: Path) -> Checkout:
    """The checkout already in *workspace*, read from ``.git/HEAD`` as a plain file: no git runs in a folder
    whose config the run writes."""
    git_dir = workspace / ".git"
    head = git_dir / "HEAD"
    if not git_dir.is_dir() or not head.is_file():
        raise CheckoutError("the dispatch's folder holds a .git that is not a checkout")
    match = _HEAD_REF.fullmatch(head.read_text(encoding="utf-8-sig", errors="replace").strip())
    return Checkout(made=False, branch=match.group(1) if match else None)


def prepare_checkout(workspace: Path, remote_url: str, repo: str, branch: str, base_branch: Optional[str],
                     base_env: Mapping[str, str], timeout: float = PREPARE_TIMEOUT_SECONDS) -> Checkout:
    """Make *workspace* a checkout of *remote_url* on *branch*, or report the checkout already there, which is
    reused as it stands. On failure *workspace* is left exactly as it was. *base_env* is the environment git
    starts from (``git_env`` makes it non-interactive)."""
    if (workspace / ".git").exists() or (workspace / ".git").is_symlink():
        return _existing_checkout(workspace)
    if workspace.exists() and any(workspace.iterdir()):
        raise CheckoutError("the dispatch's folder is not empty and holds no checkout")
    deadline = time.monotonic() + timeout
    env = git_env(base_env)
    workspace.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch = workspace.with_name(f"{SCRATCH_PREFIXES[0]}{workspace.name}-{uuid.uuid4().hex[:8]}")
    scratch.mkdir(mode=0o700)
    try:
        _git(["clone", "--quiet", "--", remote_url, "."], scratch, env, deadline, what=f"cloning {repo}")
        tracked = _git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], scratch, env,
                       deadline, what=f"looking up {branch}", ok_codes=(0, 1)) == 0
        if tracked:
            _git(["checkout", "--quiet", "-b", branch, "--track", f"origin/{branch}"], scratch, env, deadline,
                 what=f"checking out {branch}")
        else:
            start = [f"origin/{base_branch}"] if base_branch else []
            _git(["checkout", "--quiet", "-b", branch, *start], scratch, env, deadline, what=f"creating {branch}")
        if workspace.exists():
            workspace.rmdir()  # empty, checked above
        scratch.rename(workspace)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return Checkout(made=True, branch=branch)


def remove_scratch(root: Path) -> int:
    """Delete the scratch folders a stopped process left under *root*: checkouts moved aside for deletion,
    and half-made clones old enough that no clone can still be running there. Only names this module makes
    are touched. Returns how many were deleted."""
    if not root.is_dir():
        return 0
    stale_before = time.time() - _STALE_CLONE_SECONDS
    leftovers = []
    for path in root.iterdir():
        match = _SCRATCH_NAME.fullmatch(path.name)
        if match and path.is_dir() and not path.is_symlink() and (
                match.group(1) == "removing" or path.stat().st_mtime < stale_before):
            leftovers.append(path)
    for path in leftovers:
        shutil.rmtree(path, ignore_errors=True)
    return len(leftovers)
