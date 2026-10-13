"""Fresh git worktrees for new sessions, so parallel claudes in one repo don't trample each
other's working copy. Adapted from recursiveforte/chertu (chert/worktrees.py, MIT).

Each worktree starts on its own `chert/<id>` branch from the repo's current committed HEAD:
uncommitted edits, untracked files and local env files are NOT carried over. Worktrees
live under WORKTREE_ROOT/<group>/<id> (group = the project channel id, or "adhoc"), so a
session found running in one can be traced back to its project. Clean up with
`git worktree remove <path>` (or `git worktree prune` after deleting the directory).
"""
import os
import subprocess
import uuid
from pathlib import Path

WORKTREE_ROOT = Path(os.environ.get("WORKTREE_ROOT", str(Path.home() / ".chert" / "worktrees")))


def git(directory, *args):
    r = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise ValueError(f"git {args[0]}: {r.stderr.strip()[:600] or r.stdout.strip()[:600]}")
    return r.stdout.strip()


def validate_repository(directory):
    """(repo root, HEAD commit) — raises ValueError if `directory` isn't in a git repo with a commit."""
    root = Path(git(directory, "rev-parse", "--show-toplevel")).resolve()
    revision = git(directory, "rev-parse", "--verify", "HEAD^{commit}")
    return root, revision


def create_worktree(directory, group="adhoc"):
    """New worktree for `directory`'s repo; returns the path matching `directory` inside it
    (a project may point at a subdirectory of its repo)."""
    directory = Path(directory).expanduser().resolve()
    root, revision = validate_repository(directory)
    relative = directory.relative_to(root)
    if relative != Path(".") and git(directory, "cat-file", "-t", f"{revision}:{relative}") != "tree":
        raise ValueError(f"`{relative}` isn't committed in {root.name}, so a worktree wouldn't contain it")
    ident = uuid.uuid4().hex[:10]
    dest = WORKTREE_ROOT / str(group) / f"{root.name}-{ident}"
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    git(directory, "worktree", "add", "-b", f"chert/{ident}", str(dest), revision)
    return dest / relative


def worktree_group(path):
    """The group (project channel id) of a path inside WORKTREE_ROOT, else None."""
    try:
        rel = Path(path).resolve().relative_to(WORKTREE_ROOT.resolve())
    except (ValueError, OSError, TypeError):
        return None
    return rel.parts[0] if rel.parts else None
