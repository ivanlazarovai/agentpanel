"""Per-(session, agent) git worktree lifecycle.

Each panelist works in its own worktree on its own branch, so several agents can edit
the same repo simultaneously without colliding, and you can diff their results and keep
the winner. Worktrees live under ``<repo>/.agentpanel/wt/<session>/<agent>`` on branches
named ``ap/<session>/<agent>``.

This is a thin, well-tested wrapper over ``git worktree`` — no GitPython dependency.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

WT_SUBDIR = "wt"
BRANCH_PREFIX = "ap"


class GitError(RuntimeError):
    """A git command exited non-zero."""


@dataclass
class WorktreeHandle:
    """A live worktree for one (session, agent)."""

    agent: str
    path: Path
    branch: str


def _slug(value: str) -> str:
    """Filesystem/branch-safe slug (git refs forbid many characters)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "x"


async def _git(repo: Path, *args: str, check: bool = True) -> Tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    out, err = out_b.decode(errors="replace"), err_b.decode(errors="replace")
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} -> {proc.returncode}: {err.strip()}")
    return proc.returncode or 0, out, err


class WorktreeManager:
    """Creates/lists/diffs/cleans worktrees for one repo + session."""

    def __init__(self, repo: Path, session_id: str) -> None:
        self.repo = Path(repo).resolve()
        self.session_id = _slug(session_id)
        self.root = self.repo / ".agentpanel" / WT_SUBDIR / self.session_id

    def branch_for(self, agent: str) -> str:
        return f"{BRANCH_PREFIX}/{self.session_id}/{_slug(agent)}"

    def path_for(self, agent: str) -> Path:
        return self.root / _slug(agent)

    async def base_ref(self) -> str:
        """The current HEAD commit of the repo — what each worktree branches from."""
        _, out, _ = await _git(self.repo, "rev-parse", "HEAD")
        return out.strip()

    async def create(self, agent: str, base: Optional[str] = None) -> WorktreeHandle:
        """Add a worktree for ``agent`` on a fresh branch off ``base`` (default HEAD).

        Idempotent: if the worktree path already exists it is reused.
        """
        path = self.path_for(agent)
        branch = self.branch_for(agent)
        if path.exists():
            return WorktreeHandle(agent=agent, path=path, branch=branch)
        path.parent.mkdir(parents=True, exist_ok=True)
        base = base or await self.base_ref()
        # Remove a stale branch of the same name (e.g. leftover from a crashed run).
        await _git(self.repo, "branch", "-D", branch, check=False)
        await _git(self.repo, "worktree", "add", "-b", branch, str(path), base)
        return WorktreeHandle(agent=agent, path=path, branch=branch)

    async def diffstat(self, agent: str) -> str:
        """`git diff --stat` of the agent's worktree vs the session base.

        Run from inside the worktree against the base commit so it reflects both
        committed work *and* uncommitted edits the agent has made.
        """
        path = self.path_for(agent)
        base = await self.base_ref()
        _, out, _ = await _git(path, "diff", "--stat", base, check=False)
        return out.strip()

    async def diff(self, agent: str) -> str:
        """Full patch of the agent's worktree (committed + uncommitted) vs the session base."""
        path = self.path_for(agent)
        base = await self.base_ref()
        _, out, _ = await _git(path, "diff", base, check=False)
        return out

    async def has_changes(self, agent: str) -> bool:
        """True if the worktree has any work — tracked edits *or* untracked new files
        (``git diff`` alone misses untracked files, so use porcelain status)."""
        path = self.path_for(agent)
        _, out, _ = await _git(path, "status", "--porcelain", check=False)
        return bool(out.strip())

    async def commit_all(self, agent: str, message: str) -> Optional[str]:
        """Stage + commit everything in the agent's worktree. Returns the sha, or None
        if there was nothing to commit."""
        path = self.path_for(agent)
        await _git(path, "add", "-A")
        rc, _, _ = await _git(path, "diff", "--cached", "--quiet", check=False)
        if rc == 0:
            return None  # nothing staged
        await _git(path, "commit", "-m", message, "--no-verify")
        _, out, _ = await _git(path, "rev-parse", "HEAD")
        return out.strip()

    async def keep(self, agent: str, into: Optional[str] = None) -> str:
        """Merge the winning agent's branch into ``into`` (default: current repo branch).

        Returns the merge target branch name. Uses a fast-forward-friendly merge; the
        caller resolves conflicts if the base moved.
        """
        branch = self.branch_for(agent)
        if into:
            await _git(self.repo, "checkout", into)
        await _git(self.repo, "merge", "--no-ff", branch, "-m", f"AgentPanel: keep {agent}")
        _, out, _ = await _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        return out.strip()

    async def cleanup(self, agents: Optional[List[str]] = None, delete_branches: bool = True) -> None:
        """Remove worktrees (and optionally branches) for the session.

        ``agents=None`` cleans the whole session directory.
        """
        targets = agents if agents is not None else [p.name for p in self.root.glob("*") if p.is_dir()]
        for agent in targets:
            path = self.path_for(agent)
            await _git(self.repo, "worktree", "remove", "--force", str(path), check=False)
            if delete_branches:
                await _git(self.repo, "branch", "-D", self.branch_for(agent), check=False)
        await _git(self.repo, "worktree", "prune", check=False)
        if agents is None and self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
