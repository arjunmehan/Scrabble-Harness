#!/usr/bin/env python3
"""Run the step-4 native capability probe in a process with a hard deadline."""

import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from scrabble_harness.domain import (
    Board, DrawState, GameState, STANDARD_ENGLISH_INVENTORY, build_observation,
)
from scrabble_harness.engine.codec import observation_to_cgp, parse_static_moves
from scrabble_harness.engine.errors import EngineConfigurationError, InvalidRequestError
from scrabble_harness.engine.magpie_ffi import MagpieCommandError, MagpieSession

SETTINGS = ("set -lex NWL_HARNESS_SMOKE -leaves NWL_HARNESS_SMOKE "
            "-wmp false -savesettings false -shwithmoves false -threads 1 "
            "-numplays 10 -maxnumdplays 100 -seed 42")
MOVES = "addmoves 8H QI,8H qI,pass"
SIM = "simulate - -it 12 -minp 1 -plies 2 -scond none -threads 1 -seed 42"


def opening_state(*, hidden_variant: bool = False) -> GameState:
    own = tuple("?AEIQRT")
    unseen = STANDARD_ENGLISH_INVENTORY.without(own).as_counter()
    opponent = tuple("BCDFGHJ") if not hidden_variant else tuple("UUUWWYZ")
    unseen.subtract(opponent)
    bag = tuple(sorted(unseen.elements()))
    if hidden_variant:
        bag = bag[::-1]
    return GameState(
        game_id="native-spike", revision=0, board=Board(), racks=(own, opponent),
        draw_state=DrawState(ordered_tiles=bag, rng_algorithm="spike-opaque-v1",
                             rng_state="private-b" if hidden_variant else "private-a"),
        scores=(0, 0), active_player=0, scoreless_turns=0,
        ruleset_id="standard-english-spike", lexicon_id="NWL_HARNESS_SMOKE",
    )


def simulation_summary(output: str) -> dict:
    """Probe-only parser for this pin's two-ply, show_bu=false machine table."""
    rows = []
    pattern = re.compile(
        r"^\d+:\s+(pass|\(exch [A-Z?]+\)|[A-O0-9]+ [A-Za-z()]+)\s+"
        r"(?:([A-Z?]+)\s+)?(\d+)\s+([-X])\s+(.+)$"
    )
    for line in output.splitlines():
        if not re.match(r"^\d+:", line.strip()):
            continue
        match = pattern.fullmatch(line.strip())
        if not match:
            raise ValueError("Unsupported native simulation row")
        move, leave, score, ignored, metrics = match.groups()
        values = metrics.split()
        if len(values) != 12 or not all(math.isfinite(float(value)) for value in values):
            raise ValueError("Unexpected two-ply simulation metrics")
        rows.append({
            "move": move, "score": int(score), "ignored": ignored,
            "win_pct": float(values[0]), "win_pct_sem": float(values[1]),
            "equity": float(values[2]), "equity_sem": float(values[3]),
            "static_equity": float(values[4]), "samples": int(values[5]),
            "ply_statistics": [float(value) for value in values[6:]],
        })
    iterations = re.search(r"^Iters:\s+(\d+)\s*$", output, re.MULTILINE)
    status = re.search(r"^Status:\s+(.+)$", output, re.MULTILINE)
    opponent = re.search(r"^Opp Rack:[ \t]*(.*)$", output, re.MULTILINE)
    if not rows or not iterations or not status or not opponent:
        raise ValueError("Incomplete native simulation result")
    total = int(iterations[1])
    if total != sum(row["samples"] for row in rows):
        raise ValueError("Native iteration count does not match per-play samples")
    return {"rows": rows, "iterations": total, "status": status[1].strip(),
            "known_opponent_rack": opponent[1].strip()}


def verify_manifest(manifest: dict) -> None:
    lock_path = PROJECT / "configs/magpie.lock.json"
    lock = json.loads(lock_path.read_text())
    if manifest["engine_commit"] != lock["engine"]["commit"]:
        raise ValueError("Setup manifest has the wrong engine pin")
    paths = [(lock_path, manifest["lock_sha256"]),
             (Path(manifest["library"]), manifest["library_sha256"])]
    data = Path(manifest["data"])
    paths.extend((data / asset["path"], asset["sha256"]) for asset in lock["data"]["assets"])
    name = lock["fixture"]["name"]
    paths.extend([
        (data / "lexica" / f"{name}.txt", lock["fixture"]["sha256"]),
        (data / "lexica" / f"{name}.kwg", manifest["kwg_sha256"]),
        (data / "lexica" / f"{name}.klv2", manifest["klv_sha256"]),
    ])
    for path, expected in paths:
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Native artifact hash mismatch: {path}")


def probe(manifest: dict) -> dict:
    verify_manifest(manifest)
    library, data = Path(manifest["library"]), Path(manifest["data"])
    first = build_observation(opening_state(), 0)
    second = build_observation(opening_state(hidden_variant=True), 0)
    cgp = "cgp " + observation_to_cgp(first)
    checks, raw = {}, {}

    def record(name: str, passed: bool, **evidence) -> None:
        checks[name] = {"passed": bool(passed), **evidence}
        if not passed:
            raise ValueError(f"Native probe failed: {name}")

    with tempfile.TemporaryDirectory(prefix="scrabble-native-probe-") as directory:
        settings = Path(directory) / "empty-settings.txt"
        settings.touch()

        def session() -> MagpieSession:
            instance = MagpieSession(library, data, settings=settings)
            try:
                instance.run(SETTINGS)
            except BaseException:
                instance.close()
                raise
            return instance

        with session() as native:
            version = native.run("version").strip()
            native.run(cgp)
            raw["generated"] = native.run("generate")
            generated = parse_static_moves(raw["generated"])
            record("generation", len(generated) == 10 and generated[0].score == 22,
                   candidate_count=len(generated), first_move=asdict(generated[0]))

            native.run(cgp)
            before = native.run("shgame")
            native.run(MOVES)
            raw["restricted_static"] = native.run("shmoves")
            preview = parse_static_moves(raw["restricted_static"])
            scores = {row.notation: row.score for row in preview}
            record("preview_score_and_isolation", scores == {"8H QI": 22, "8H qI": 2, "pass": 0}
                   and native.run("shgame") == before, scores=scores)
            raw["bounded_simulation"] = native.run(SIM)
            bounded = simulation_summary(raw["bounded_simulation"])
            record("restricted_bounded_analysis", bounded["iterations"] == 12
                   and {row["move"] for row in bounded["rows"]} == set(scores)
                   and all(row["samples"] >= 1 for row in bounded["rows"]),
                   result=bounded, work_definition="one candidate rollout sample; total across candidates")

            with session() as defaults:
                defaults.run(cgp)
                defaults.run(MOVES)
                raw["default_sample_floor"] = defaults.run(SIM.replace(" -minp 1", ""))
            floor = simulation_summary(raw["default_sample_floor"])
            record("minimum_sample_floor", floor["iterations"] == 1500,
                   requested_iterations=12, minimum_per_candidate=500,
                   actual_iterations=floor["iterations"])

            native.run(cgp)
            raw["phony_preview"] = native.run("addmoves 8H QA")
            raw["phony_commit"] = native.run("commit 8H QA")
            raw["board_after_phony_commit"] = native.run("shgame")
            record("phonies_require_host_gate", "Invalid words formed" in raw["phony_preview"]
                   and "Invalid words formed" in raw["phony_commit"]
                   and "QA" in raw["board_after_phony_commit"],
                   native_exit_code=0, committed_despite_phony=True)

            native.run(cgp.replace("?AEIQRT/", "AEINRST/"))
            native.run("addmoves 8E RETINAS")
            raw["bingo_preview"] = native.run("shmoves")
            record("bingo_score", parse_static_moves(raw["bingo_preview"])[0].score == 64)

            native.run(cgp)
            native.run("addmoves ex AE?")
            raw["exchange_preview"] = native.run("shmoves")
            exchange = parse_static_moves(raw["exchange_preview"])[0]
            record("exchange_notation", exchange.notation == "(exch AE?)" and exchange.score == 0)

            native.run(cgp)
            before_error = native.run("shgame")
            try:
                native.run("cgp invalid ?AEIQRT/ 0/0 0")
            except MagpieCommandError as exc:
                error_code, details = exc.exit_code, exc.details
            else:
                raise ValueError("Expected native CGP parse error")
            record("error_clear_and_reuse", bool(details) and native.run("shgame") == before_error
                   and native.run("version").strip() == version, exit_code=error_code)

        # Fresh independent handles: equality must not be an old-result cache.
        outputs = []
        transcripts = []
        for observation in (first, second, first):
            transcript = [SETTINGS, "cgp " + observation_to_cgp(observation), MOVES, SIM]
            transcripts.append(transcript)
            with session() as native:
                native.run(transcript[1])
                native.run(MOVES)
                outputs.append(native.run(SIM))
        results = [simulation_summary(output) for output in outputs]
        raw["hidden_variant_simulations"] = outputs
        record("unknown_rack_and_seed_repeatability",
               first.observation_id == second.observation_id
               and transcripts[0] == transcripts[1] == transcripts[2]
               and results[0] == results[1] == results[2]
               and not results[0]["known_opponent_rack"],
               observation_id=first.observation_id,
               identical_native_requests=True, identical_quantitative_results=True,
               private_fields_changed=["opponent rack", "bag composition", "bag order", "RNG state"],
               transcript=transcripts[0], seed=42, threads=1)

        # Demonstrate that omitting '-' uses stored private offturn tiles.
        with session() as native:
            native.run(cgp.replace("?AEIQRT/", "?AEIQRT/BCDFGHJ"))
            native.run(MOVES)
            raw["known_rack_simulation"] = native.run(SIM.replace("simulate - ", "simulate "))
            known = simulation_summary(raw["known_rack_simulation"])
            record("known_rack_default_is_unsafe_for_public_analysis",
                   known["known_opponent_rack"] == "BCDFGHJ",
                   stored_rack_used=known["known_opponent_rack"])

            native.run(cgp.replace("?AEIQRT/", "?AEIQRT/BCDFGHJ"))
            raw["private_position_before_commit"] = native.run("shgame")
            native.run("commit 8H QI")
            raw["private_position_after_commit"] = native.run("shgame")
            before, after = raw["private_position_before_commit"], raw["private_position_after_commit"]
            record("commit_is_not_a_complete_arbiter_transition",
                   "AEIQRT?" in before and "BCDFGHJ" in before
                   and re.search(r"Player 1\s+22", after) is not None
                   and re.search(r"Player 2\s+0", after) is not None,
                   score_after=22, both_native_racks_empty_after_commit=True,
                   implication="History-oriented commit does not retain/refill the CGP racks; host must own game transitions")

        with session() as native:
            native.run(cgp)
            native.run(MOVES)
            native.start(SIM.replace("-it 12", "-it 100000000"))
            try:
                native.output()
            except InvalidRequestError:
                guarded = True
            else:
                guarded = False
            # Wait for nonzero work in this fresh handle, not a previous status.
            deadline = time.monotonic() + 5
            live = ""
            while time.monotonic() < deadline:
                live = native.status()
                count = re.search(r"^Iters:\s+(\d+)\s*$", live, re.MULTILINE)
                if count and int(count[1]) > 0:
                    break
                time.sleep(0.01)
            started = time.monotonic()
            native.stop()
            raw["cancelled_simulation"] = native.await_output()
            cancel_seconds = time.monotonic() - started
            interrupted = simulation_summary(raw["cancelled_simulation"])
            record("cancellation_and_handle_reuse", guarded
                   and interrupted["status"] == "Finished (user interrupt)"
                   and 0 < interrupted["iterations"] < 100000000
                   and native.run("version").strip() == version,
                   cancel_to_join_seconds=cancel_seconds, result=interrupted)
        native.close()  # Idempotent, no double free.

        try:
            MagpieSession(library, Path(directory) / "missing-data", settings=settings)
        except EngineConfigurationError:
            creation_failed_safely = True
        else:
            creation_failed_safely = False
        record("creation_failure", creation_failed_safely)

    return {
        "schema_version": 1, "purpose": "interface smoke probe, not playing-strength evaluation",
        "setup": manifest, "reported_engine_version": version,
        "all_checks_passed": all(value["passed"] for value in checks.values()),
        "checks": checks, "raw_output": raw,
        "unsupported_or_unverified": {
            "draw_preserving_native_checkpoint": "C API has no bag-order/RNG capture or restore functions; CGP/GCG are not full private checkpoints",
            "standalone_native_arbiter": "commit replays game history and clears unspecified subsequent racks; it is not a complete draw-preserving game transition",
            "production_lexicon_and_leaves": "Toy lexicon, zero KLV; no strength conclusions",
            "endgame_and_partial_unknown_racks": "Not verified by this smoke probe; public codec rejects these",
            "analysis_and_arbiter_protocol_implementations": "Step 5; this probe uses internal command binding only",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=PROJECT / ".native/manifest.json")
    parser.add_argument("--report", type=Path, default=PROJECT / "docs/native-spike-results.json")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.timeout > 0 or not math.isfinite(args.timeout):
        parser.error("timeout must be positive and finite")
    if args._child:
        report = probe(json.loads(args.manifest.read_text()))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Passed {len(report['checks'])} native checks; report: {args.report}")
        return
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__)), "--_child", "--manifest",
             str(args.manifest.resolve()), "--report", str(args.report.resolve())],
            capture_output=True, text=True, timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        print("Native probe exceeded its deadline; child terminated", file=sys.stderr)
        raise SystemExit(1)
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr[-6000:], file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
