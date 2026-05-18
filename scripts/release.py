#!/usr/bin/env python3.11
"""Release omega-memory to PyPI.

Alternative path to the GH Actions publish.yml workflow on omega-public.
The script pushes a git tag but does not create a GitHub release, so the
auto-publish workflow does not fire — no double-publish risk.

Use when you don't want to wait for or trust GitHub Actions runners.

Usage:
    python3.11 scripts/release.py <version>            # publish for real
    python3.11 scripts/release.py <version> --dry-run  # build + verify only
    python3.11 scripts/release.py <version> --skip-confirm  # CI-like, no prompts

Pre-flight:
    - PYPI_TOKEN_OMEGA in ~/.omega/secrets.json
    - Working tree clean on main, up to date with origin
    - Version not already tagged
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"
INIT_PY = REPO / "src" / "omega" / "__init__.py"
SECRETS = Path.home() / ".omega" / "secrets.json"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=REPO, check=True, **kw)


def step(name: str) -> None:
    print(f"\n=== {name} ===")


def confirm(prompt: str, skip: bool) -> None:
    if skip:
        return
    answer = input(f"\n{prompt} [y/N] ").strip().lower()
    if answer != "y":
        sys.exit("Aborted.")


def preflight(version: str) -> None:
    step("Pre-flight")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"Version must be X.Y.Z, got {version!r}")

    if not SECRETS.exists():
        sys.exit(f"Missing {SECRETS}")
    secrets = json.loads(SECRETS.read_text())
    if not secrets.get("PYPI_TOKEN_OMEGA"):
        sys.exit("PYPI_TOKEN_OMEGA not in ~/.omega/secrets.json")

    # Only block on uncommitted changes to files this script will modify.
    tracked_targets = ["pyproject.toml", "src/omega/__init__.py"]
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--"] + tracked_targets,
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if dirty:
        sys.exit(f"Uncommitted changes to release-target files:\n{dirty}")

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if branch != "main":
        sys.exit(f"Not on main, on {branch!r}")

    run(["git", "fetch", "origin", "main"])
    behind = subprocess.run(
        ["git", "rev-list", "--count", "HEAD..origin/main"], cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if behind != "0":
        sys.exit(f"Local main is {behind} commits behind origin/main. Pull first.")

    tag = f"v{version}"
    existing = subprocess.run(
        ["git", "tag", "-l", tag], cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if existing:
        sys.exit(f"Tag {tag} already exists locally.")

    print(f"  OK: version={version}, branch=main, clean, no tag {tag}")


def bump_version(version: str) -> None:
    step(f"Bumping version to {version}")
    for path, pattern, replacement in [
        (PYPROJECT, r'^version = "[^"]+"', f'version = "{version}"'),
        (INIT_PY, r'^__version__ = "[^"]+"', f'__version__ = "{version}"'),
    ]:
        text = path.read_text()
        if not re.search(pattern, text, flags=re.MULTILINE):
            sys.exit(f"Pattern not found in {path}: {pattern!r}")
        new = re.sub(pattern, replacement, text, count=1, flags=re.MULTILINE)
        if new == text:
            print(f"  unchanged {path.relative_to(REPO)}: already at {version}")
        else:
            path.write_text(new)
            print(f"  updated {path.relative_to(REPO)}")


def build() -> tuple[Path, Path]:
    step("Building wheel + sdist")
    dist = REPO / "dist"
    if dist.exists():
        for f in dist.iterdir():
            f.unlink()
    run([sys.executable, "-m", "build", "--wheel", "--sdist"])
    wheels = list(dist.glob("omega_memory-*.whl"))
    sdists = list(dist.glob("omega_memory-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        sys.exit(f"Expected 1 wheel + 1 sdist, got {wheels=} {sdists=}")
    return wheels[0], sdists[0]


def verify(wheel: Path, expected_version: str) -> None:
    step("Verifying wheel in clean venv")
    with tempfile.TemporaryDirectory(prefix="omega-mem-verify-") as tmp:
        env_dir = Path(tmp) / "venv"
        venv.create(str(env_dir), with_pip=True)
        py = env_dir / "bin" / "python3.11"
        if not py.exists():
            py = env_dir / "bin" / "python"
        run([str(py), "-m", "pip", "install", "--quiet", str(wheel)])
        proc = subprocess.run(
            [str(py), "-c", "import omega; print(omega.__version__)"],
            capture_output=True, text=True, check=True,
        )
        installed = proc.stdout.strip()
        if installed != expected_version:
            sys.exit(f"Wheel reports {installed!r}, expected {expected_version!r}")
        print(f"  OK: installed and reported version={installed}")


def publish_pypi(wheel: Path, sdist: Path) -> None:
    step("Publishing to PyPI")
    secrets = json.loads(SECRETS.read_text())
    env = {**os.environ, "TWINE_USERNAME": "__token__", "TWINE_PASSWORD": secrets["PYPI_TOKEN_OMEGA"]}
    subprocess.run(
        [sys.executable, "-m", "twine", "upload", "--non-interactive", str(wheel), str(sdist)],
        env=env, check=True,
    )
    print(f"  Published: https://pypi.org/project/omega-memory/{wheel.stem.split('-')[1]}/")


def git_commit_tag_push(version: str) -> None:
    step("Committing + tagging + pushing")
    run(["git", "add", "pyproject.toml", "src/omega/__init__.py"])
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if staged:
        run(["git", "commit", "-m", f"chore: release v{version}"])
    else:
        print("  no version-file changes to commit (idempotent re-run)")
    existing = subprocess.run(
        ["git", "tag", "-l", f"v{version}"], cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not existing:
        run(["git", "tag", f"v{version}"])
    run(["git", "push", "origin", "main"])
    run(["git", "push", "origin", f"v{version}"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version", help="Version X.Y.Z")
    ap.add_argument("--dry-run", action="store_true", help="Build + verify only; do not publish or push")
    ap.add_argument("--skip-confirm", action="store_true", help="Skip interactive confirmation")
    args = ap.parse_args()

    preflight(args.version)
    bump_version(args.version)
    wheel, sdist = build()
    verify(wheel, args.version)

    if args.dry_run:
        print(f"\nDRY RUN: would publish {wheel.name} + {sdist.name} and push v{args.version}")
        print("Reverting version bump...")
        run(["git", "checkout", "--", "pyproject.toml", "src/omega/__init__.py"])
        return 0

    confirm(f"Publish omega-memory {args.version} to PyPI?", args.skip_confirm)
    publish_pypi(wheel, sdist)

    confirm(f"Commit + tag v{args.version} + push to origin/main?", args.skip_confirm)
    git_commit_tag_push(args.version)

    step("Done")
    print(f"omega-memory {args.version} released.")
    print(f"  PyPI:   https://pypi.org/project/omega-memory/{args.version}/")
    print(f"  GitHub: https://github.com/omega-memory/omega-memory/releases/tag/v{args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
