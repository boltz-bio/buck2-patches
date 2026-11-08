#!/usr/bin/env python3
"""Run one mixed native test command with stock Buck2 and retain outputs via its test SDK."""

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
PROTOS = {
    "buck2_test_proto": ["test.proto"],
    "buck2_data": ["data.proto", "error.proto"],
    "buck2_host_sharing_proto": ["host_sharing.proto"],
}
DEFS = """
def _impl(ctx):
    return [
        DefaultInfo(),
        ExternalRunnerTestInfo(type = "custom", command = ["/bin/sh", "-c", ctx.attrs.command]),
    ]

probe = rule(impl = _impl, attrs = {"command": attrs.string()})
"""
TARGETS = """
load(":defs.bzl", "probe")
probe(name = "pass", command = 'printf passed > "$CAPTURES/image.bin"')
probe(name = "fail", command = 'printf failed > "$CAPTURES/image.bin"; exit 7')
probe(name = "empty", command = 'test -d "$CAPTURES"')
probe(name = "no_output", command = 'test -z "${CAPTURES:-}"')
"""


def check(source: Path, binary: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="buck2-stock-executor-") as temporary:
        root = Path(temporary)
        proto_dirs = {key: source / "app" / key for key in PROTOS}
        subprocess.run([
            sys.executable, "-m", "grpc_tools.protoc",
            *[f"-I{path}" for path in proto_dirs.values()],
            f"--python_out={root}",
            f"--grpc_python_out={root}",
            *[str(proto_dirs[key] / name) for key, names in PROTOS.items() for name in names],
        ], check=True)
        launcher = root / "executor"
        launcher.write_text(
            "#!/bin/sh\nexec "
            + shlex.join([sys.executable, str(ROOT / "stock_test_executor.py")])
            + ' "$@"\n'
        )
        launcher.chmod(0o755)
        (root / ".buckroot").touch()
        (root / ".buckconfig").write_text(
            "[cells]\nroot = .\n[buildfile]\nname = BUCK\n"
            f"[test]\nv2_test_executor = {launcher}\n"
        )
        (root / "defs.bzl").write_text(DEFS)
        (root / "BUCK").write_text(TARGETS)
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join(
            path for path in [str(root), os.environ.get("PYTHONPATH")] if path
        )}
        retained = root / "retained"
        try:
            result = subprocess.run(
                [str(binary), "test", "--test-executor-stderr=-", "//:", "--", str(retained)],
                cwd=root, env=environment, timeout=90, capture_output=True, text=True,
            )
            if result.returncode != 32:
                raise RuntimeError(result.stdout + result.stderr)
            expected = [
                ["root//:empty", "PASS", str(retained / "empty")],
                ["root//:fail", "FAIL", str(retained / "fail")],
                ["root//:no_output", "PASS", None],
                ["root//:pass", "PASS", str(retained / "pass")],
            ]
            assert json.loads((retained / "results.json").read_text()) == expected
            assert (retained / "pass/image.bin").read_bytes() == b"passed"
            assert (retained / "fail/image.bin").read_bytes() == b"failed"
            assert (retained / "empty").is_dir() and not list((retained / "empty").iterdir())
            assert not (retained / "no_output").exists()
        finally:
            subprocess.run([str(binary), "kill"], cwd=root, timeout=15, check=True)
    print("Stock test SDK retained pass, fail, empty and absent outputs from one native command.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--buck2", type=Path, required=True)
    arguments = parser.parse_args()
    check(arguments.source.resolve(), arguments.buck2.resolve())


if __name__ == "__main__":
    main()
