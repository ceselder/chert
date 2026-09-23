#!/usr/bin/env python3
"""Ash Twin Project — the harness's memory across loops (S3 backup + disk offload).

In Outer Wilds the Ash Twin Project is the machine that carries memories through the
supernova. Here it is the piece that keeps the box's Claude state alive when the disk
fills up or dies:

  backup      sync ~/.claude essentials (transcripts, registry-adjacent config, the bridge
              state, CLAUDE.md) to s3://BUCKET/backup/<host>/ — changed files only
  disk        free space + the biggest directories under $HOME + cold offload candidates
  candidates  big directories nobody has touched in a while — what to `offload`
  offload P   copy directory P to s3://BUCKET/offload/<relpath>/, verify every object,
              THEN delete it locally and leave P.OFFLOADED.md behind with the restore command
  restore P   bring an offloaded directory back
  status      what is in the bucket, by prefix

Everything here is also importable — discord_bot.py calls these for `!disk`, `!offload`,
`!restore`, `!backup` and the disk watchdog. boto3 is imported lazily so the bridge still
starts on a box without it (S3 features then fail with a readable message).

Config (env): S3_BUCKET (required for anything S3; unset = Ash Twin disabled), AWS creds from ~/.aws or env,
ASH_HOME (default $HOME), ASH_MANIFEST (default ~/.claude/ash-twin-manifest.json).
"""
import concurrent.futures as cf
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

BUCKET = os.environ.get("S3_BUCKET", "")   # your private bucket; blank disables every S3 feature
HOME = Path(os.environ.get("ASH_HOME", str(Path.home())))
CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", str(HOME / ".claude")))
MANIFEST = Path(os.environ.get("ASH_MANIFEST", str(CLAUDE_HOME / "ash-twin-manifest.json")))
HOST = socket.gethostname()
HERE = Path(__file__).resolve().parent
MARKER_SUFFIX = ".OFFLOADED.md"
# Never offload these even if asked: the harness would stop working.
PROTECTED = {HOME, CLAUDE_HOME, HOME / ".ssh", HOME / ".aws", HERE, HOME / "shared"}
_CACHE = {}


def _s3():
    if not BUCKET:
        raise RuntimeError("S3_BUCKET is not set — Ash Twin (S3 backup/offload) is disabled; "
                           "set S3_BUCKET in .env and give this box AWS credentials to enable it")
    import boto3  # lazy: the bridge must start even if boto3 is missing
    return boto3.client("s3")


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


# ---------- disk ----------

def disk_free(path=None):
    """(free_gb, used_pct) for the filesystem holding `path` (default $HOME)."""
    du = shutil.disk_usage(str(path or HOME))
    return du.free / 1e9, 100.0 * du.used / du.total


def _du_top(root, timeout=240):
    """[(path, bytes)] for every top-level entry under root, biggest first."""
    entries = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
    if not entries:
        return []
    rc = subprocess.run(["du", "-xs", "--", *map(str, entries)], capture_output=True,
                        text=True, timeout=timeout)
    out = []
    for line in rc.stdout.splitlines():
        kb, _, p = line.partition("\t")
        if kb.isdigit():
            out.append((Path(p), int(kb) * 1024))
    out.sort(key=lambda t: -t[1])
    return out


def _newest_mtime(path, timeout=60):
    """Most recent file mtime under path (dir mtimes lie: they only track direct children)."""
    try:
        rc = subprocess.run(["find", str(path), "-xdev", "-type", "f", "-printf", "%T@\n"],
                            capture_output=True, text=True, timeout=timeout)
        vals = [float(x) for x in rc.stdout.split() if x]
        return max(vals) if vals else path.stat().st_mtime
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return path.stat().st_mtime


def disk_report(max_age=1800, top=12):
    """Free space, biggest dirs under $HOME, cold candidates. Cached for max_age seconds
    because a full `du` of a 200 GB home takes a minute or two."""
    now = time.time()
    hit = _CACHE.get("report")
    if hit and now - hit["at"] < max_age:
        rep = dict(hit["rep"])
        rep["free_gb"], rep["used_pct"] = disk_free()
        return rep
    free_gb, used_pct = disk_free()
    big = _du_top(HOME)[:top]
    rows = []
    for p, size in big:
        age_days = (now - _newest_mtime(p)) / 86400
        rows.append({"path": str(p), "bytes": size, "age_days": round(age_days, 1),
                     "offloaded": (p.parent / (p.name + MARKER_SUFFIX)).exists()})
    rep = {"free_gb": free_gb, "used_pct": used_pct, "top": rows, "host": HOST,
           "tmp_bytes": sum(s for _, s in _du_top(Path("/tmp"))) if Path("/tmp").exists() else 0}
    _CACHE["report"] = {"at": now, "rep": rep}
    return rep


def candidates(min_gb=1.0, cold_days=30, rep=None):
    """Offload candidates: big AND cold AND not protected. Biggest first."""
    rep = rep or disk_report()
    out = []
    for r in rep["top"]:
        p = Path(r["path"])
        if r["bytes"] < min_gb * 1e9 or r["age_days"] < cold_days or r["offloaded"]:
            continue
        if p in PROTECTED or p.name.startswith("."):
            continue
        out.append(r)
    return out


def format_report(rep, cands=None, n=8):
    """Discord/CLI-friendly text for a disk report."""
    lvl = "🕳️" if rep["free_gb"] < 5 else "🌋" if rep["free_gb"] < 15 else "🌤️"
    lines = [f"{lvl} **{rep['free_gb']:.1f} GB free** · {rep['used_pct']:.0f}% used on `{rep['host']}`"
             + (f" · /tmp {human(rep['tmp_bytes'])}" if rep.get("tmp_bytes") else "")]
    for r in rep["top"][:n]:
        tag = " · offloaded" if r["offloaded"] else ""
        lines.append(f"-# {human(r['bytes']):>9} · {r['age_days']:>5.0f}d cold · `{r['path']}`{tag}")
    if cands is None:
        cands = candidates(rep=rep)
    if cands:
        lines.append("**cold & big — offload candidates** (copies to S3, verifies, then frees the disk):")
        for r in cands[:5]:
            lines.append(f"`!offload {r['path']}` — {human(r['bytes'])}, untouched {r['age_days']:.0f}d")
    return "\n".join(lines)


# ---------- S3 helpers ----------

def _walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for f in filenames:
            p = Path(dirpath) / f
            if p.is_symlink() or not p.is_file():
                continue
            yield p


def _list_prefix(s3, prefix):
    """{key: size} for everything under prefix."""
    out = {}
    token = None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            out[o["Key"]] = o["Size"]
        if not r.get("IsTruncated"):
            return out
        token = r.get("NextContinuationToken")


def _upload_many(s3, pairs, workers=8, progress=None):
    """pairs: [(local Path, key)]. Returns [(path, key, error|None)]."""
    from boto3.s3.transfer import TransferConfig
    cfg = TransferConfig(multipart_threshold=64 * 1024 * 1024, max_concurrency=4)
    done, results = 0, []

    def one(p, key):
        try:
            s3.upload_file(str(p), BUCKET, key, Config=cfg)
            return p, key, None
        except Exception as e:  # noqa: BLE001 — report per file, never abort the batch
            return p, key, f"{type(e).__name__}: {e}"

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(lambda pk: one(*pk), pairs):
            results.append(res)
            done += 1
            if progress and (done % 50 == 0 or done == len(pairs)):
                progress(done, len(pairs))
    return results


# ---------- backup (the actual Ash Twin: memories across loops) ----------

BACKUP_SETS = [
    # (local path, recursive, glob)
    (CLAUDE_HOME / "projects", True, "*.jsonl"),     # transcripts = session history
    (CLAUDE_HOME / "sessions", True, "*.json"),      # registry snapshot (which sids were live)
    (CLAUDE_HOME / "CLAUDE.md", False, None),        # the system prompt
    (CLAUDE_HOME / "settings.json", False, None),
    (CLAUDE_HOME / "history.jsonl", False, None),    # prompt history
    (HOME / ".claude.json", False, None),            # trust map, onboarding, primaryApiKey
    (HERE / "bot_state.json", False, None),          # thread <-> session map
    (HERE / ".env", False, None),                    # bridge config (bucket is private + SSE)
]


def _backup_files():
    for base, recursive, pattern in BACKUP_SETS:
        if not base.exists():
            continue
        if not recursive:
            yield base
            continue
        for p in base.rglob(pattern):
            if p.is_file():
                yield p


def _rel_key(p):
    try:
        rel = p.relative_to(HOME)
    except ValueError:
        rel = Path(*p.parts[1:])
    return f"backup/{HOST}/{rel.as_posix()}"


def backup(progress=print, force=False):
    """Upload every backup file whose (size, mtime) changed since the last run."""
    s3 = _s3()
    try:
        manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    except (OSError, json.JSONDecodeError):
        manifest = {}
    pairs, sigs = [], {}
    for p in _backup_files():
        try:
            st = p.stat()
        except OSError:
            continue
        sig = f"{st.st_size}:{int(st.st_mtime)}"
        key = _rel_key(p)
        sigs[key] = sig
        if force or manifest.get(key) != sig:
            pairs.append((p, key))
    if not pairs:
        progress(f"ash twin: nothing changed ({len(sigs)} files tracked)")
        return {"uploaded": 0, "failed": 0, "tracked": len(sigs), "bytes": 0}
    total = sum(p.stat().st_size for p, _ in pairs)
    progress(f"ash twin: uploading {len(pairs)} changed files ({human(total)}) to s3://{BUCKET}/backup/{HOST}/")
    results = _upload_many(s3, pairs, progress=lambda d, n: progress(f"  {d}/{n}"))
    failed = [(p, e) for p, _, e in results if e]
    for p, key, err in results:
        if err:
            sigs.pop(key, None)  # retry next time
    # keep entries for files that vanished locally out of the manifest so they re-upload if they return
    manifest = {k: v for k, v in sigs.items()}
    try:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        tmp = MANIFEST.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest))
        tmp.replace(MANIFEST)
    except OSError as e:
        progress(f"  manifest not saved ({e}) — next run re-uploads")
    for p, e in failed[:5]:
        progress(f"  FAILED {p}: {e}")
    progress(f"ash twin: done — {len(pairs) - len(failed)} uploaded, {len(failed)} failed")
    return {"uploaded": len(pairs) - len(failed), "failed": len(failed),
            "tracked": len(sigs), "bytes": total}


# ---------- offload / restore ----------

def _offload_prefix(path):
    rel = path.resolve().relative_to(HOME) if path.resolve().is_relative_to(HOME) \
        else Path(*path.resolve().parts[1:])
    return f"offload/{HOST}/{rel.as_posix()}/"


def plan_offload(path):
    """What `offload` would do, without doing it. Raises ValueError if refused."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = HOME / p
    p = p.resolve()
    if not p.is_dir():
        raise ValueError(f"{p} is not a directory")
    if p in PROTECTED or any(p == pp or pp.is_relative_to(p) for pp in PROTECTED):
        raise ValueError(f"{p} is protected — it holds the harness or its keys")
    if not p.is_relative_to(HOME):
        raise ValueError(f"{p} is outside {HOME}; only home directories are offloaded")
    if (p.parent / (p.name + MARKER_SUFFIX)).exists():
        raise ValueError(f"{p} already has an offload marker — `restore` it first")
    files = list(_walk(p))
    size = 0
    for f in files:
        try:
            size += f.stat().st_size
        except OSError:
            pass
    dirty = None
    if (p / ".git").exists():
        rc = subprocess.run(["git", "-C", str(p), "status", "--porcelain"],
                            capture_output=True, text=True, timeout=60)
        dirty = len(rc.stdout.splitlines()) if rc.returncode == 0 else None
    return {"path": str(p), "files": len(files), "bytes": size,
            "prefix": _offload_prefix(p), "git_dirty": dirty}


def offload(path, progress=print):
    """Copy to S3, verify sizes, delete locally, leave a marker. Returns a summary dict."""
    plan = plan_offload(path)
    p = Path(plan["path"])
    s3 = _s3()
    prefix = plan["prefix"]
    files = list(_walk(p))
    pairs = [(f, prefix + f.relative_to(p).as_posix()) for f in files]
    progress(f"offload: {len(pairs)} files, {human(plan['bytes'])} → s3://{BUCKET}/{prefix}")
    results = _upload_many(s3, pairs, progress=lambda d, n: progress(f"  uploaded {d}/{n}"))
    failed = [(f, e) for f, _, e in results if e]
    if failed:
        for f, e in failed[:5]:
            progress(f"  FAILED {f}: {e}")
        raise RuntimeError(f"{len(failed)} uploads failed — nothing deleted locally")
    # verify: every local file must exist remotely with the same size
    remote = _list_prefix(s3, prefix)
    bad = []
    for f, key in pairs:
        try:
            if remote.get(key) != f.stat().st_size:
                bad.append(key)
        except OSError:
            bad.append(key)
    if bad:
        raise RuntimeError(f"verification failed for {len(bad)} objects (e.g. {bad[0]}) — nothing deleted locally")
    marker = p.parent / (p.name + MARKER_SUFFIX)
    marker.write_text(
        f"# {p.name} was offloaded to S3\n\n"
        f"- when: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}\n"
        f"- where: `s3://{BUCKET}/{prefix}`\n"
        f"- size: {human(plan['bytes'])} in {len(pairs)} files (verified object-by-object)\n"
        f"- git status at offload: {plan['git_dirty']} dirty paths\n\n"
        f"Restore with:\n\n    python3 {HERE / 'ash_twin.py'} restore {p}\n\n"
        f"or from Discord: `!restore {p} confirm`\n")
    shutil.rmtree(p)
    progress(f"offload: verified {len(pairs)} objects, removed {p}, marker at {marker}")
    return {**plan, "marker": str(marker), "uploaded": len(pairs)}


def restore(path, progress=print):
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = HOME / p
    marker = p.parent / (p.name + MARKER_SUFFIX)
    prefix = _offload_prefix(p)
    s3 = _s3()
    remote = _list_prefix(s3, prefix)
    if not remote:
        raise ValueError(f"nothing in s3://{BUCKET}/{prefix}")
    if p.exists() and any(p.iterdir()):
        raise ValueError(f"{p} exists and is not empty — refusing to overwrite")
    total = sum(remote.values())
    progress(f"restore: {len(remote)} objects, {human(total)} → {p}")
    free_gb, _ = disk_free()
    if total / 1e9 > free_gb - 2:
        raise RuntimeError(f"not enough free space ({free_gb:.1f} GB) for {human(total)}")
    p.mkdir(parents=True, exist_ok=True)
    done = 0

    def one(key):
        dest = p / key[len(prefix):]
        dest.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(BUCKET, key, str(dest))
        return key

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for _ in ex.map(one, remote):
            done += 1
            if done % 50 == 0 or done == len(remote):
                progress(f"  downloaded {done}/{len(remote)}")
    if marker.exists():
        marker.unlink()
    progress(f"restore: done — {p} is back ({human(total)})")
    return {"path": str(p), "files": len(remote), "bytes": total}


def status():
    s3 = _s3()
    out = {}
    for pref in ("backup/", "offload/"):
        objs = _list_prefix(s3, pref)
        by_top = {}
        for k, sz in objs.items():
            parts = k.split("/")
            top = "/".join(parts[:3]) if pref == "offload/" else "/".join(parts[:2])
            by_top[top] = by_top.get(top, 0) + sz
        out[pref] = {"objects": len(objs), "bytes": sum(objs.values()), "by_top": by_top}
    return out


def format_status(st):
    lines = [f"🪐 `s3://{BUCKET}`"]
    for pref, d in st.items():
        lines.append(f"**{pref}** {d['objects']} objects · {human(d['bytes'])}")
        for top, sz in sorted(d["by_top"].items(), key=lambda t: -t[1])[:8]:
            lines.append(f"-# {human(sz):>9} · `{top}`")
    return "\n".join(lines)


# ---------- CLI ----------

def main(argv):
    cmd = argv[1] if len(argv) > 1 else "disk"
    if cmd == "disk":
        rep = disk_report()
        print(format_report(rep).replace("**", "").replace("-# ", "  ").replace("`", ""))
    elif cmd == "candidates":
        for r in candidates():
            print(f"{human(r['bytes']):>9}  {r['age_days']:>5.0f}d  {r['path']}")
    elif cmd == "backup":
        backup(force="--force" in argv)
    elif cmd == "plan":
        print(json.dumps(plan_offload(argv[2]), indent=1))
    elif cmd == "offload":
        print(json.dumps(offload(argv[2]), indent=1))
    elif cmd == "restore":
        print(json.dumps(restore(argv[2]), indent=1))
    elif cmd == "status":
        print(format_status(status()).replace("**", "").replace("-# ", "  ").replace("`", ""))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)


# ---------- transcripts that only survive in the backup ----------

_TX_CACHE = {}


def backup_transcripts(max_age=600):
    """{sid: {"key", "size", "modified", "slug"}} for every top-level session transcript in
    s3://BUCKET/backup/<host>/.claude/projects/<slug>/<sid>.jsonl. Claude Code deletes local
    transcripts after `cleanupPeriodDays`; the backup never deletes, so this is the long
    memory `!resume` searches when a session is gone from disk."""
    if not BUCKET:
        return {}          # no S3 configured: no long memory, and nothing to log about it
    now = time.time()
    hit = _TX_CACHE.get("rows")
    if hit and now - hit["at"] < max_age:
        return hit["rows"]
    prefix = f"backup/{HOST}/.claude/projects/"
    rows = {}
    try:
        s3 = _s3()
        token = None
        while True:
            kw = {"Bucket": BUCKET, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            r = s3.list_objects_v2(**kw)
            for o in r.get("Contents", []):
                rest = o["Key"][len(prefix):].split("/")
                if len(rest) == 2 and rest[1].endswith(".jsonl"):
                    rows[rest[1][:-6]] = {"key": o["Key"], "size": o["Size"], "slug": rest[0],
                                          "modified": o["LastModified"].timestamp()}
            if not r.get("IsTruncated"):
                break
            token = r.get("NextContinuationToken")
    except Exception as e:  # noqa: BLE001 — no S3 = no long memory, not a crash
        print(f"backup_transcripts: {e}", file=sys.stderr)
    _TX_CACHE["rows"] = {"at": now, "rows": rows}
    return rows


def restore_transcript(sid):
    """Download one session's transcript back to ~/.claude/projects/<slug>/<sid>.jsonl so
    `claude -r <sid>` can find it. Returns the local path."""
    row = backup_transcripts().get(sid)
    if not row:
        raise ValueError(f"no backup for session {sid}")
    dest = CLAUDE_HOME / "projects" / row["slug"] / f"{sid}.jsonl"
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".jsonl.part")
    _s3().download_file(BUCKET, row["key"], str(tmp))
    tmp.replace(dest)
    return dest
