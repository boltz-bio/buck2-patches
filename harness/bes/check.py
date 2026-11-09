# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is dual-licensed under either the MIT license found in the
# LICENSE-MIT file in the root directory of this source tree or the Apache
# License, Version 2.0 found in the LICENSE-APACHE file in the root directory
# of this source tree. You may select, at your option, one of the
# above-listed licenses.

"""Run buck2 against a recording Build Event Service and check what it received.

    python3 check.py --buck2 <path to buck2> --buck2-src <patched buck2 checkout>

The Python running this needs `grpcio` and `grpcio-tools`. It generates the
stubs from the protos under the checkout's `remote_execution/oss`, starts
`fake_bes_server.py`, points this project's `[bes]` section at it, and checks
four commands:

- a build that succeeds streams `started` first and `build_finished` with
  SUCCESS, then closes the stream;
- a build that fails reports a non-zero exit code in `build_finished`;
- a test run reports each test's status;
- `buck2 kill --bes-drain-timeout` waits until a slow server has read and
  acknowledged a stream the daemon still held when its command ended.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROTOS = Path()


def generate_stubs(out: Path) -> None:
    bes = PROTOS / "bes_grpc_proto" / "proto"
    google = PROTOS / "google_grpc_proto" / "proto"
    bep = PROTOS / "bazel_bep_proto"
    sources = [
        *bes.glob("*.proto"),
        *(google / "google" / "api").glob("*.proto"),
        google / "google" / "rpc" / "status.proto",
        *(bep / "proto").glob("*.proto"),
    ]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{bes}",
            f"-I{google}",
            f"-I{bep}",
            f"--python_out={out}",
            f"--grpc_python_out={out}",
            *map(str, sources),
        ],
        check=True,
    )


class Server:
    def __init__(self, work: Path, name: str, *flags: str) -> None:
        self.record = work / f"{name}.jsonl"
        port_file = work / f"{name}.port"
        self.log = open(work / f"{name}.log", "w")
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(HERE / "fake_bes_server.py"),
                f"--stubs={work / 'stubs'}",
                f"--record={self.record}",
                f"--port-file={port_file}",
                *flags,
            ],
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 30
        while not port_file.exists():
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(
                    f"fake BES server did not start, see {self.log.name}"
                )
            time.sleep(0.1)
        self.port = int(port_file.read_text())

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=30)
        self.log.close()

    def stream(self, build_id: str) -> list:
        rows = [json.loads(line) for line in self.record.read_text().splitlines()]
        return [row for row in rows if row.get("invocation_id") == build_id]


class Check:
    def __init__(self, buck2: str, work: Path) -> None:
        self.buck2 = buck2
        self.work = work
        self.failures = []
        self.commands = 0
        self.isolation_dir = work.name

    def configure(self, server: Server) -> None:
        (HERE / ".buckconfig.local").write_text(
            "[bes]\n"
            f"  backend = grpc://127.0.0.1:{server.port}\n"
            "  event_format = bazel\n"
            "  bazel_artifact_upload = false\n"
        )

    def run(self, *args: str) -> tuple:
        self.commands += 1
        build_id_file = self.work / f"build-id-{self.commands}"
        result = subprocess.run(
            [self.buck2, "--isolation-dir", self.isolation_dir, *args, f"--write-build-id={build_id_file}"],
            cwd=HERE,
            capture_output=True,
            text=True,
        )
        build_id = build_id_file.read_text().strip()
        print(
            f"$ buck2 {' '.join(args)}: exit {result.returncode}, build ID {build_id}"
        )
        return result.returncode, build_id

    def kill(self, *args: str) -> str:
        result = subprocess.run(
            [self.buck2, "--isolation-dir", self.isolation_dir, "kill", *args],
            cwd=HERE,
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr
        print(f"$ buck2 kill {' '.join(args)}: exit {result.returncode}")
        for line in output.splitlines():
            if "BES:" in line:
                print(f"  {line.strip()}")
        return output

    def expect(self, ok: bool, what: str) -> None:
        print(f"  {'ok' if ok else 'FAILED'}: {what}")
        if not ok:
            self.failures.append(what)

    def expect_complete_stream(self, stream: list, exit_ok: bool) -> None:
        events = [row for row in stream if row["rpc"] == "stream"]
        self.expect(
            bool(events), f"the server received the stream ({len(events)} events)"
        )
        if not events:
            return
        seqs = [row["seq"] for row in events]
        self.expect(
            seqs == list(range(1, len(seqs) + 1)),
            "sequence numbers run from 1 without a gap",
        )
        self.expect(
            events[0].get("bazel_id") == "started", "the first event is `started`"
        )
        finished = [row for row in events if row.get("bazel_id") == "build_finished"]
        self.expect(len(finished) == 1, "one `build_finished` event")
        if finished:
            code = finished[0].get("exit_code", 0)
            self.expect(
                (code == 0) == exit_ok,
                f"`build_finished` reports exit code {code} "
                f"({finished[0].get('exit_name')})",
            )
        last_messages = [index for index, row in enumerate(events) if row.get("last_message")]
        bazel_events = [index for index, row in enumerate(events) if row.get("bazel_id")]
        self.expect(
            len(last_messages) == 1 and bool(bazel_events) and last_messages[0] == bazel_events[-1],
            "exactly the final BEP event is marked `last_message`",
        )
        self.expect(
            events[-1]["kind"] == "component_stream_finished",
            "the stream ends with `component_stream_finished`",
        )
        self.expect(
            any(row["rpc"] == "stream_end" for row in stream),
            "the client closed the stream",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--buck2", required=True)
    parser.add_argument("--buck2-src", required=True)
    args = parser.parse_args()
    global PROTOS
    PROTOS = Path(args.buck2_src).resolve() / "remote_execution" / "oss"

    work = Path(tempfile.mkdtemp(prefix="buck2-bes-check-"))
    (work / "stubs").mkdir()
    generate_stubs(work / "stubs")
    check = Check(os.path.abspath(args.buck2), work)
    check.kill()

    server = Server(work, "acks")
    check.configure(server)
    try:
        rc, analysis_id = check.run("bxl", "//:analysis.bxl:main")
        check.expect(rc == 0, "analysis-only BXL succeeds")
        rc, build_id = check.run("build", "//:ok")
        check.expect(rc == 0, "`buck2 build //:ok` succeeds")
        build_ok = build_id
        rc, build_id = check.run("build", "//:fail")
        check.expect(rc != 0, "`buck2 build //:fail` fails")
        build_fail = build_id
        rc, action_less_id = check.run("build", "//:data")
        check.expect(rc == 0, "`buck2 build //:data` succeeds")
        rc, dependency_id = check.run("build", "//:uses_dep")
        check.expect(rc == 0, "`buck2 build //:uses_dep` succeeds")
        # A new daemon analyses again, so the converter learns the requested targets; one that
        # keeps its analysis from an earlier command reports none.
        check.kill("--bes-drain-timeout=60s")
        rc, rebuilt_dependency_id = check.run("build", "//:uses_dep")
        check.expect(rc == 0, "`buck2 build //:uses_dep` in a new daemon succeeds")
        check.kill("--bes-drain-timeout=60s")
        # More patterns than the 512 KiB other event consumers see, the requested target last.
        many_patterns = work / "many-patterns"
        many_patterns.write_text("//:data\n" * 80_000 + "//:uses_dep\n")
        rc, many_patterns_id = check.run("build", f"@{many_patterns}")
        check.expect(rc == 0, "a build naming 80,001 patterns succeeds")
        check.kill("--bes-drain-timeout=60s")
        rc, unbuilt_id = check.run("test", "//:test_pass", "//:ok")
        check.expect(rc == 0, "`buck2 test //:test_pass //:ok` succeeds without building `//:ok`")
        rc, configurations_id = check.run("build", "//:tool", "//:uses_tool")
        check.expect(rc == 0, "`buck2 build //:tool //:uses_tool` succeeds")
        rc, build_id = check.run("test", "//:test_pass", "//:test_fail")
        check.expect(rc != 0, "`buck2 test` fails, as `//:test_fail` does")
        test = build_id
        rc, cached_test_id = check.run("test", "//:test_pass")
        check.expect(rc == 0, "a warm test run succeeds")
        check.kill("--bes-drain-timeout=60s")
    finally:
        server.stop()

    print("A build that succeeds:")
    check.expect_complete_stream(server.stream(build_ok), exit_ok=True)
    print("A build that fails:")
    check.expect_complete_stream(server.stream(build_fail), exit_ok=False)
    summaries = [row for row in server.stream(build_ok) if row.get("bazel_id") == "target_summary"]
    check.expect(len(summaries) == 1 and summaries[0].get("overall_build_success"),
                 "the real build has one successful target summary")
    print("A build of a target with no actions:")
    action_less = server.stream(action_less_id)
    check.expect_complete_stream(action_less, exit_ok=True)
    completed = [row for row in action_less if row.get("bazel_id") == "target_completed"]
    check.expect([(row.get("label"), row.get("completed_success")) for row in completed] == [("//:data", True)],
                 "the target with no actions completes successfully")
    posted = {row["event_id_bytes"] for row in action_less if "event_id_bytes" in row}
    check.expect(all(child in posted
                     for row in action_less if row.get("bazel_payload") == "configured"
                     for child in row.get("children_id_bytes", [])),
                 "every configured target's completion is posted, so none stays building")
    print("Only requested targets get target events:")
    for name, build_id in [("built", dependency_id), ("rebuilt", rebuilt_dependency_id)]:
        recorded = server.stream(build_id)
        check.expect_complete_stream(recorded, exit_ok=True)
        labels = {row["label"] for row in recorded
                  if row.get("bazel_id") in {"target_configured", "target_completed"} and row.get("label")}
        check.expect(labels == {"//:uses_dep"},
                     f"a {name} dependency gets no target events (target events for {sorted(labels)})")
    many = server.stream(many_patterns_id)
    check.expect_complete_stream(many, exit_ok=True)
    many_completed = {row["label"]: row.get("completed_success") for row in many
                      if row.get("bazel_id") == "target_completed" and row.get("label")}
    check.expect(many_completed == {"//:data": True, "//:uses_dep": True},
                 f"the last of 80,001 patterns still names a requested target (completed {many_completed})")
    unbuilt = server.stream(unbuilt_id)
    check.expect_complete_stream(unbuilt, exit_ok=True)
    ok_completions = [row for row in unbuilt if row.get("bazel_id") == "target_completed" and row.get("label") == "//:ok"]
    check.expect(len(ok_completions) == 1 and ok_completions[0].get("aborted_reason") == "SKIPPED",
                 "a requested target the test command did not build closes as skipped, not built")
    configurations = server.stream(configurations_id)
    check.expect_complete_stream(configurations, exit_ok=True)
    tool_completions = [row for row in configurations
                        if row.get("bazel_id") == "target_completed" and row.get("label") == "//:tool"]
    tool_configured = [row for row in configurations
                       if row.get("bazel_payload") == "configured" and row.get("label") == "//:tool"]
    print(f"  `//:tool` completed in {len({row.get('configuration') for row in tool_completions})} configuration(s)")
    check.expect(bool(tool_completions) and all(row.get("completed_success") for row in tool_completions),
                 "every configuration of `//:tool` completes successfully")
    check.expect(bool(tool_configured)
                 and {row["event_id_bytes"] for row in tool_completions}
                 <= set(tool_configured[-1].get("children_id_bytes", [])),
                 "one TargetConfigured announces the completion of every configuration")
    analysis = server.stream(analysis_id)
    check.expect_complete_stream(analysis, exit_ok=True)
    check.expect(not any(row.get("bazel_id") in {"target_completed", "target_summary"} for row in analysis),
                 "analysis-only BXL reports no completed build targets")
    configured = [row for row in analysis if row.get("bazel_payload") == "configured"]
    announced = {child for row in analysis for child in row.get("children_id_bytes", [])}
    check.expect(bool(configured) and all(row["event_id_bytes"] in announced for row in configured),
                 "analysis metadata is retained and announced in the event graph")
    warm_tests = server.stream(cached_test_id)
    check.expect_complete_stream(warm_tests, exit_ok=True)
    check.expect(any(row.get("bazel_id") == "test_summary" and row.get("test_status") == "PASSED"
                     for row in warm_tests), "warm test status is preserved")
    for recorded in [server.stream(build_ok), server.stream(build_fail), analysis, warm_tests]:
        logs = [row for row in recorded if row.get("bazel_id") == "build_tool_logs"]
        check.expect(len(logs) == 1, "BuildToolLogs is emitted once at final closure")
        check.expect(bool(logs) and {"command", "exit_code", "outcome", "timings_ms", "tool"}.issubset(logs[0].get("invocation_json_keys", [])),
                     "the final invocation artifact preserves command diagnostics")
        ids = [row["event_id_bytes"] for row in recorded
               if row.get("bazel_id") in {"target_completed", "target_summary"}]
        check.expect(len(ids) == len(set(ids)), "each target completion and summary ID is final once")
    print("A test run:")
    stream = server.stream(test)
    check.expect_complete_stream(stream, exit_ok=False)
    statuses = {
        row["label"]: row["test_status"]
        for row in stream
        if row.get("bazel_id") == "test_result"
    }
    check.expect(
        statuses.get("//:test_pass") == "PASSED",
        f"`//:test_pass` is PASSED (results: {statuses})",
    )
    check.expect(
        statuses.get("//:test_fail") == "FAILED",
        "`//:test_fail` is FAILED",
    )

    # This server reads each event late and acknowledges only at the end of a
    # stream, so the build returns while the daemon still holds most of its
    # events, and only a drain delivers them before the daemon exits.
    server = Server(work, "slow", "--ack-at-eof", "--read-delay=0.1")
    check.configure(server)
    try:
        rc, analysis_id = check.run("bxl", "//:analysis.bxl:main")
        check.expect(rc == 0, "analysis-only BXL succeeds")
        rc, build_id = check.run("build", "//:ok")
        check.expect(rc == 0, "`buck2 build //:ok` succeeds")
        output = check.kill("--bes-drain-timeout=120s")
    finally:
        server.stop()

    print("`buck2 kill --bes-drain-timeout` after a build, with a slow server:")
    drained = re.search(
        r"BES: (\d+) of (\d+) streams acknowledged, (\d+) events pending", output
    )
    check.expect(drained is not None, "the kill reports the drain")
    if drained:
        acked, total, pending = map(int, drained.groups())
        check.expect(total >= 1, f"the daemon still held {total} stream(s) at the kill")
        check.expect(
            acked == total and pending == 0, "the server acknowledged them all"
        )
    check.expect_complete_stream(server.stream(build_id), exit_ok=True)

    (HERE / ".buckconfig.local").unlink()
    if check.failures:
        print(f"{len(check.failures)} check(s) failed; the recordings are in {work}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
