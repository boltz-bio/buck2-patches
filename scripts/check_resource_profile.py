#!/usr/bin/env python3
"""Verify daemon CPU/RSS counters in a real profile received over BES.

Use the Python environment used by examples/bes/check.py (grpcio and
grpcio-tools installed). The build and server use a temporary project.
Artifact uploading is disabled so the recorder can decode inline logs.
"""

import argparse
import concurrent.futures
import gzip
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile


def check_profile(contents: bytes) -> None:
    events = json.loads(gzip.decompress(contents))["traceEvents"]
    counters = [event for event in events if event.get("ph") == "C"]
    cpu = [
        event for event in counters
        if event["name"] == "CPU usage (Buck2 daemon, cores)"
    ]
    rss = [
        event for event in counters
        if event["name"] == "Resident memory (Buck2 daemon, MiB)"
    ]
    assert cpu, "live CPU counters missing"
    assert rss, "live RSS counters missing"
    timed = [event["ts"] for event in events if "ts" in event]
    assert timed == sorted(timed) and min(timed) >= 0
    assert all(
        value >= 0 for event in counters for value in event["args"].values()
    )
    assert any(
        event.get("ph") == "X" and event.get("cat") == "command"
        for event in events
    ), "command span missing"
    assert any(
        event.get("ph") == "X" and event.get("cat") == "action"
        for event in events
    ), "action span missing"
    print(json.dumps({
        "cpu_samples": len(cpu),
        "rss_samples": len(rss),
        "first_cpu": cpu[0],
        "last_rss": rss[-1],
        "total_events": len(events),
    }, sort_keys=True))


def run(source: pathlib.Path, binary: pathlib.Path, work: pathlib.Path) -> None:
    stubs = work / "stubs"
    stubs.mkdir()
    spec = importlib.util.spec_from_file_location(
        "bes_check", source / "examples/bes/check.py"
    )
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    check.generate_stubs(stubs)
    sys.path.insert(0, str(stubs))
    import grpc
    import publish_build_event_pb2
    import publish_build_event_pb2_grpc
    from google.protobuf import empty_pb2
    from proto import build_event_stream_pb2

    profiles = []

    class Recorder(publish_build_event_pb2_grpc.PublishBuildEventServicer):
        def PublishLifecycleEvent(self, request, context):
            return empty_pb2.Empty()

        def PublishBuildToolEventStream(self, requests, context):
            for request in requests:
                ordered = request.ordered_build_event
                event = build_event_stream_pb2.BuildEvent()
                if (
                    ordered.event.HasField("bazel_event")
                    and ordered.event.bazel_event.Unpack(event)
                    and event.HasField("build_tool_logs")
                ):
                    for entry in event.build_tool_logs.log:
                        if entry.name == "command.profile.gz":
                            assert entry.WhichOneof("file") == "contents", entry
                            profiles.append(entry.contents)
                yield publish_build_event_pb2.PublishBuildToolEventStreamResponse(
                    stream_id=ordered.stream_id,
                    sequence_number=ordered.sequence_number,
                )

    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=8))
    publish_build_event_pb2_grpc.add_PublishBuildEventServicer_to_server(
        Recorder(), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    project = work / "project"
    project.mkdir()
    shutil.copy(source / "examples/bes/.buckconfig", project)
    shutil.copytree(source / "examples/bes/toolchains", project / "toolchains")
    (project / ".buckconfig.local").write_text(
        "[bes]\n"
        f"backend = grpc://127.0.0.1:{port}\n"
        "event_format = bazel\n"
        "bazel_artifact_upload = false\n"
    )
    (project / "BUCK").write_text(
        'oncall("build_infra")\n'
        'genrule(name="metrics", out="ok.txt", '
        'cmd="sleep 5; echo ok > $OUT")\n'
    )
    try:
        subprocess.run([str(binary), "build", "//:metrics"], cwd=project, check=True)
    finally:
        try:
            subprocess.run(
                [str(binary), "kill", "--bes-drain-timeout=60s"],
                cwd=project,
                check=True,
            )
        finally:
            server.stop(0).wait()
    assert profiles, "no command.profile.gz received over BES"
    for contents in profiles:
        check_profile(contents)
    print("Live BES daemon resource counter smoke PASS")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=pathlib.Path)
    parser.add_argument("--buck2", required=True, type=pathlib.Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="buck2-resource-profile-") as temporary:
        run(args.source.resolve(), args.buck2.resolve(), pathlib.Path(temporary))


if __name__ == "__main__":
    main()
