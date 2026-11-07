#!/usr/bin/env python3
"""Verify retained native test outputs and actual statuses through the public built-in runner and its versioned manifest."""

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def run(binary: Path, fixture: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary), "--isolation-dir=native-output-check", *arguments],
        cwd=fixture,
        capture_output=True,
        text=True,
        check=False,
    )


def check(source: Path, binary: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="native-test-outputs-") as temporary:
        root = Path(temporary)
        fixture = root / "fixture"
        shutil.copytree(
            source / "examples/bes",
            fixture,
            ignore=shutil.ignore_patterns("buck-out", ".buckconfig.local", "__pycache__"),
        )
        (fixture / "BUCK").write_text(
            'oncall("build_infra")\n'
            'sh_test(name="capture_pass", test="capture.sh", args=["0"])\n'
            'sh_test(name="capture_fail", test="capture.sh", args=["7"])\n'
        )
        script = fixture / "capture.sh"
        script.write_text(
            '#!/bin/sh\nset -eu\n'
            'printf "%s\\n" "retained native output" > "$CAPTURES/image.bin"\n'
            'exit "$1"\n'
        )
        script.chmod(0o755)
        try:
            for case, target, expected_exit, expected_status in [
                ("cold", "capture_pass", 0, "PASS"),
                ("warm", "capture_pass", 0, "PASS"),
                ("failed", "capture_fail", 32, "FAIL"),
            ]:
                manifest = root / f"{case}.outputs"
                result = run(
                    binary,
                    fixture,
                    "test",
                    f"//:{target}",
                    "--",
                    "--output-env",
                    "CAPTURES=captures",
                    "--outputs-manifest",
                    str(manifest),
                )
                if result.returncode != expected_exit:
                    raise RuntimeError(
                        f"{case}: native test exited {result.returncode}, expected {expected_exit}\n"
                        + result.stdout + result.stderr
                    )
                fields = manifest.read_bytes().split(b"\0")
                if fields[-1] != b"" or fields[0] != b"buck2-test-outputs-v1" or len(fields) != 6:
                    raise RuntimeError(f"{case}: invalid output manifest framing")
                label, status, name, path = (field.decode("utf-8") for field in fields[1:-1])
                if (label, status, name) != (f"root//:{target}", expected_status, "captures"):
                    raise RuntimeError(f"{case}: manifest does not preserve the native target/status")
                directory = Path(path)
                if not directory.is_absolute() or not directory.resolve().is_relative_to(Path(str(manifest) + ".d").resolve()):
                    raise RuntimeError(f"{case}: output path is outside this manifest's retained directory")
                if (directory / "image.bin").read_bytes() != b"retained native output\n":
                    raise RuntimeError(f"{case}: declared output was not materialized intact")
                stale = run(
                    binary, fixture, "test", f"//:{target}", "--",
                    "--output-env", "CAPTURES=captures", "--outputs-manifest", str(manifest),
                )
                if stale.returncode == 0:
                    raise RuntimeError("An existing retained output directory was accepted")
                if (directory / "image.bin").read_bytes() != b"retained native output\n":
                    raise RuntimeError("An existing retained output directory was changed")
                if case == "cold":
                    # Delete only our own SDK-reported output, so the warm run must restore it.
                    shutil.rmtree(directory)
        finally:
            stopped = run(binary, fixture, "kill", "--bes-drain-timeout=10s")
            if stopped.returncode != 0:
                raise RuntimeError("The probe's native daemon did not stop: " + stopped.stderr)
    print("Native output checks passed: cold, warm, and failed test statuses and materialized files")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--buck2", type=Path, required=True)
    arguments = parser.parse_args()
    check(arguments.source.resolve(), arguments.buck2.resolve())


if __name__ == "__main__":
    main()
