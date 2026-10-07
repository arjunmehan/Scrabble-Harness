#!/usr/bin/env python3
"""Build the pinned library and tiny fixture data, without upstream setup.sh."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_file(path: Path, expected: str) -> None:
    if digest(path) != expected:
        raise ValueError(f"SHA-256 mismatch: {path}")


def command(args: list[str], *, cwd: Path | None = None, timeout: int = 180) -> str:
    result = subprocess.run(args, cwd=cwd, check=True, capture_output=True,
                            text=True, timeout=timeout)
    return result.stdout.strip()


def prepare_data(library: Path, data: Path, name: str) -> None:
    from scrabble_harness.engine.magpie_ffi import MagpieSession
    with tempfile.TemporaryDirectory(prefix="scrabble-native-settings-") as directory:
        settings = Path(directory) / "empty-settings.txt"
        settings.touch()
        with MagpieSession(library, data, settings=settings) as session:
            session.run(f"convert text2kwg {name} english")
            session.run(f"create klv {name} english")


def bootstrap(output: Path, source: Path | None, asset_source: Path | None) -> dict:
    lock = json.loads((PROJECT / "configs/magpie.lock.json").read_text())
    if platform.system() not in {"Darwin", "Linux"}:
        raise ValueError("This spike setup supports macOS/Linux only")
    output.mkdir(parents=True, exist_ok=True)
    source = source or output / "MAGPIE"
    pin = lock["engine"]["commit"]
    if not source.exists():
        source.mkdir(parents=True)
        command(["git", "init", str(source)])
        command(["git", "remote", "add", "origin", lock["engine"]["repository"]], cwd=source)
        command(["git", "fetch", "--depth", "1", "origin", pin], cwd=source)
        command(["git", "checkout", "--detach", pin], cwd=source)
    if command(["git", "rev-parse", "HEAD"], cwd=source) != pin:
        raise ValueError("Engine checkout differs from the lock; use a separate clean checkout")
    if command(["git", "status", "--porcelain", "--untracked-files=no"], cwd=source):
        raise ValueError("Engine checkout has tracked modifications")
    # Clean only the verified checkout's ignored build outputs. Prevent reuse of
    # objects compiled with other board/rack flags, which make cannot detect.
    command(["make", "clean"], cwd=source)
    command(lock["engine"]["build_command"], cwd=source)
    suffix = "dylib" if platform.system() == "Darwin" else "so"
    library = source / "bin" / f"libmagpie.{suffix}"
    data = output / "data"
    hashes = {}
    for asset in lock["data"]["assets"]:
        destination = data / asset["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if asset_source:
            content = (asset_source / asset["path"]).read_bytes()
        else:
            url = ("https://raw.githubusercontent.com/jvc56/MAGPIE-DATA/"
                   f"{lock['data']['commit']}/data/{asset['path']}")
            with urllib.request.urlopen(url, timeout=30) as response:
                content = response.read()
        if hashlib.sha256(content).hexdigest() != asset["sha256"]:
            raise ValueError(f"Asset SHA-256 mismatch: {asset['path']}")
        destination.write_bytes(content)
        hashes[asset["path"]] = asset["sha256"]
    fixture = lock["fixture"]
    lexicon = PROJECT / fixture["path"]
    checked_file(lexicon, fixture["sha256"])
    (data / "lexica").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(lexicon, data / "lexica" / lexicon.name)
    # Native conversion is also isolated and bounded; no library in this parent.
    command([sys.executable, str(Path(__file__)), "--_prepare-data",
             "--library", str(library), "--data", str(data)], timeout=30)
    manifest = {
        "schema_version": 1, "engine_commit": pin,
        "data_commit": lock["data"]["commit"],
        "lock_sha256": digest(PROJECT / "configs/magpie.lock.json"),
        "platform": platform.platform(), "architecture": platform.machine(),
        "compiler": command(["cc", "--version"]).splitlines()[0],
        "build_command": lock["engine"]["build_command"],
        "library": str(library.resolve()), "library_sha256": digest(library),
        "data": str(data.resolve()), "input_asset_sha256": hashes,
        "lexicon_id": fixture["name"],
        "lexicon_sha256": fixture["sha256"],
        "kwg_sha256": digest(data / "lexica" / f"{fixture['name']}.kwg"),
        "klv_sha256": digest(data / "lexica" / f"{fixture['name']}.klv2"),
        "strength_benchmark": False,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT / ".native")
    parser.add_argument("--engine-source", type=Path, help="Reuse a clean checkout at the exact pin")
    parser.add_argument("--asset-source", type=Path, help="Offline root with the three locked data files")
    parser.add_argument("--_prepare-data", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--library", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--data", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args._prepare_data:
            lock = json.loads((PROJECT / "configs/magpie.lock.json").read_text())
            prepare_data(args.library, args.data, lock["fixture"]["name"])
        else:
            manifest = bootstrap(args.output.resolve(),
                                 args.engine_source.resolve() if args.engine_source else None,
                                 args.asset_source.resolve() if args.asset_source else None)
            print(f"Built {manifest['engine_commit']} ({manifest['architecture']})")
            print(f"Setup manifest: {args.output.resolve() / 'manifest.json'}")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print(exc.stderr[-4000:], file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
