"""Offline checks for fixed fixtures, written with AI assistance. Python 3.11+."""

import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parent
ORACLE_MODULES = (
    "determinex_local_oracle.py",
    "determinex_io_extractor.py",
    "intake/__init__.py",
    "intake/hardened_runner.py",
)
SYNTH = "determinex_synthesize.py"
CORPUS = "bit_techniques_playbook.json"


def source_paths():
    # The submission ships the CLI oracle in src/scripts and the two fixtures in
    # verify_src. The full Determinex checkout uses scripts/ and corpus/ directly.
    packaged = ROOT / "src" / "scripts"
    scripts = packaged if packaged.is_dir() else ROOT / "scripts"
    override = os.environ.get("DETERMINEX_VERIFY_SRC")
    if override:
        fixtures = (ROOT / override).resolve()
        return scripts, fixtures, fixtures / CORPUS
    if scripts == packaged:
        fixtures = ROOT / "verify_src"
        return scripts, fixtures, fixtures / CORPUS
    return scripts, scripts, ROOT / "corpus" / CORPUS


def check_oracle():
    from determinex_local_oracle import Example, run_oracle

    examples = [
        Example(test="double " + str(n), argv=[str(n)], expect_rc=0, expect_stdout=f"{n * 2}\n")
        for n in (0, 2, 7)
    ]
    # run_oracle compares stdout and exit codes. Require the right failure
    # class on every case; missing tools or a crash must not validate a fixture.
    cases = [
        ("correct", "print(n * 2)", 3, {}),
        ("syntax-error", "print(n * )", 0, {"rc": 3}),
        ("wrong-answer", "print(42)", 0, {"exact": 3}),
    ]
    with TemporaryDirectory(prefix="determinex-candidates-") as temp:
        for label, body, expected_passed, expected_failures in cases:
            path = Path(temp) / (label + ".py")
            path.write_text("import sys\nn = int(sys.argv[1])\n" + body + "\n", encoding="utf-8")
            if label == "wrong-answer":
                compile(path.read_bytes(), str(path), "exec")
            detail = io.StringIO()
            with contextlib.redirect_stdout(detail):
                passed, total, failures = run_oracle(path, examples=examples, label="double")
            result = f"{label} {passed}/{total} accepted; failure classes {dict(failures)}"
            print("  " + result)
            if (passed, total, failures) != (expected_passed, len(examples), expected_failures):
                print(detail.getvalue())
                return "unexpected oracle verdict for " + label
            if label == "syntax-error":
                diagnostic = Example(test="syntax diagnosis", argv=["2"], expect_rc=1, expect_in=["SyntaxError"])
                with contextlib.redirect_stdout(detail):
                    diagnosed, count, errors = run_oracle(path, examples=[diagnostic], label="syntax diagnosis")
                if (diagnosed, count, errors) != (1, 1, {}):
                    print(detail.getvalue())
                    return "missing SyntaxError from the broken fixture"
    return None


def check_spec(synth_dir):
    # The local oracle also adds its own directory to sys.path. Load the exact
    # selected synthesizer so a different on-disk copy cannot conceal a fault.
    path = synth_dir / SYNTH
    loader = importlib.util.spec_from_file_location("determinex_synthesize", path)
    synth = importlib.util.module_from_spec(loader)
    sys.modules[loader.name] = synth
    loader.loader.exec_module(synth)

    for description, expected_count, expected_vacuous in (
        ("improve the code quality of my project", 0, True),
        ("solution(numbers) returns the average. For example solution([1, 2, 3]) "
         "returns 2.0, solution([10]) returns 10.0, and solution([]) returns 0.0.", 3, False),
    ):
        spec = synth.parse_spec(description)
        vacuous = synth.oracle_is_vacuous(synth.synthesize_oracle_tests(spec))
        print(f"  {len(spec.examples)} examples; symbol-only oracle: {vacuous}")
        if (len(spec.examples), vacuous) != (expected_count, expected_vacuous):
            return "unexpected synthesized test for " + ("vague" if expected_vacuous else "specific") + " spec"
    return None


def check_references(corpus):
    expectations = {
        "population_count": ("popcount", lambda x: x.bit_count()),
        "count_leading_zeros": ("clz", lambda x: 8 - x.bit_length()),
        "is_power_of_two": ("is_pow2", lambda x: x in {1, 2, 4, 8, 16, 32, 64, 128}),
        "reverse_bits": ("reverse", lambda x: int(f"{x:08b}"[::-1], 2)),
    }
    entries = json.loads(corpus.read_text(encoding="utf-8"))["entries"]
    for key, (function, expected) in expectations.items():
        matches = [entry for entry in entries if entry["id"] == key]
        if len(matches) != 1:
            raise ValueError(f"expected one reference for {key}; found {len(matches)}")
        namespace = {}
        exec(matches[0]["reference"], namespace)
        for x in range(256):
            observed = namespace[function](x, 8)
            if observed != expected(x):
                print(f"  {function}({x}, 8) returned {observed}; expected {expected(x)}")
                return "incorrect reference: " + function
    print(f"  {len(expectations)} of {len(entries)} entries checked: popcount, clz, is_pow2, reverse")
    print(f"  {len(expectations) * 256} comparisons across all 256 unsigned 8-bit inputs")
    return None


CHECKS = ("oracle", "spec", "references")


def run_checks(scripts, synth_dir, corpus):
    sys.path.insert(0, str(scripts))
    checks = (check_oracle, lambda: check_spec(synth_dir), lambda: check_references(corpus))
    success = True
    for name, check in zip(CHECKS, checks, strict=True):
        try:
            if name == "oracle" and not all((scripts / f).is_file() for f in ORACLE_MODULES):
                raise FileNotFoundError(f"incomplete Determinex local oracle at {scripts}")
            failure = check()
        except Exception as exc:
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
            success = False
            continue
        if failure is None:
            print(f"PASS {name}")
        else:
            print(f"FAIL {name}: {failure}")
            success = False
    return 0 if success else 1


def replace_once(path, old, new):
    source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise ValueError(f"mutation expected exactly one match in {path.name}; found {source.count(old)}")
    path.write_text(source.replace(old, new), encoding="utf-8")


def change_reverse(path):
    book = json.loads(path.read_text(encoding="utf-8"))
    matches = [entry for entry in book["entries"] if entry["id"] == "reverse_bits"]
    if len(matches) != 1:
        raise ValueError("expected exactly one reverse_bits entry")
    matches[0]["reference"] = "def reverse(x, bits): return x"
    path.write_text(json.dumps(book), encoding="utf-8")


def selftest(scripts, synth_dir, corpus):
    # Start green. Each independent copy contains only the real inputs used in
    # this run. Fresh subprocesses prevent module caches crossing mutations.
    if run_checks(scripts, synth_dir, corpus):
        print("Self-test aborted: the baseline must pass all checks.")
        return 1
    mutations = [
        ("oracle", Path("src/scripts") / ORACLE_MODULES[0],
         lambda p: replace_once(p, "if ex.expect_stdout is not None and out != ex.expect_stdout:",
                                "if False:  # ignore exact stdout"),
         "unexpected oracle verdict for wrong-answer"),
        ("spec", Path("verify_src") / SYNTH,
         lambda p: replace_once(p, 'return VACUOUS_ORACLE_MARKER in (test_code or "")', "return False"),
         "unexpected synthesized test for vague spec"),
        ("references", Path("verify_src") / CORPUS, change_reverse, "incorrect reference: reverse"),
    ]
    child_env = os.environ.copy()
    child_env.pop("DETERMINEX_VERIFY_SRC", None)
    for name, relative, mutate, failure in mutations:
        with TemporaryDirectory(prefix="determinex-control-") as temp:
            scratch = Path(temp)
            shutil.copyfile(__file__, scratch / "verify.py")
            for item in ORACLE_MODULES:
                dest = scratch / "src/scripts" / item
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(scripts / item, dest)
            fixtures = scratch / "verify_src"
            fixtures.mkdir()
            shutil.copyfile(synth_dir / SYNTH, fixtures / SYNTH)
            shutil.copyfile(corpus, fixtures / CORPUS)
            target = scratch / relative
            before = target.read_bytes()
            mutate(target)
            if target.read_bytes() == before:
                raise ValueError(f"mutation made no change to {relative}")
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(scratch / "verify.py")],
                env=child_env, capture_output=True, text=True, timeout=120,
            )
            observed = [line for line in result.stdout.splitlines()
                        if line.startswith(("PASS ", "FAIL ", "ERROR "))]
            expected = [f"FAIL {name}: {failure}" if key == name else f"PASS {key}" for key in CHECKS]
            if result.returncode != 1 or observed != expected:
                print(f"Control {name} failed to isolate its intended fault.\n{result.stdout}{result.stderr}")
                return 1
            print("Control caught: " + " | ".join(observed))
    print(f"{len(mutations)} targeted mutations caught.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11+ is required")
    scripts, synth_dir, corpus = source_paths()
    if args.selftest:
        return selftest(scripts, synth_dir, corpus)
    return run_checks(scripts, synth_dir, corpus)


if __name__ == "__main__":
    # Translate SIGTERM to an exit that unwinds active TemporaryDirectory blocks.
    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    sys.dont_write_bytecode = True
    sys.exit(main())
