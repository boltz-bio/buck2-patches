"""Fixture-only external executor used by check_stock_test_executor.py."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import select
from shutil import copytree
import socket
import sys
from threading import Thread

import grpc
import test_pb2 as pb
import test_pb2_grpc as rpc


def relay(left, right):
    with left, right:
        peers = {left: right, right: left}
        try:
            while True:
                ready, _, _ = select.select(list(peers), [], [])
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    peers[source].sendall(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Executor(rpc.TestExecutorServicer):
    def __init__(self):
        self.requests = queue.Queue()

    def ExternalRunnerSpec(self, request, context):
        self.requests.put(request.test_spec)
        return pb.Empty()

    def EndOfTestRequests(self, request, context):
        self.requests.put(None)
        return pb.Empty()


def run_test(client, spec, retained):
    def arg(value):
        return pb.ArgValue(content=pb.ArgValueContent(spec_value=value))

    name = spec.target.target
    label = f"{spec.target.cell}//{spec.target.package}:{name}"
    executable = pb.TestExecutable(
        stage=pb.TestStage(testing=pb.Testing(suite=name)),
        target=spec.target.handle,
        cmd=[arg(value) for value in spec.command],
        env=[pb.EnvironmentVariable(key=key, value=arg(value)) for key, value in spec.env.items()],
    )
    if name != "no_output":
        executable.pre_create_dirs.add(name="capture")
        executable.env.add(
            key="CAPTURES",
            value=pb.ArgValue(content=pb.ArgValueContent(declared_output=pb.OutputName(name="capture"))),
        )
    request = pb.ExecuteRequest2(test_executable=executable)
    request.timeout.seconds = 10
    request.host_sharing_requirements.shared.weight_class.permits = 1
    response = client.Execute2(request, timeout=30)
    result = response.result
    assert response.HasField("result") and result.status.HasField("finished"), response
    status = pb.PASS if result.status.finished == 0 else pb.FAIL
    capture = None
    if executable.pre_create_dirs:
        assert len(result.outputs) == 1, result
        output = result.outputs[0]
        assert output.declared_output.name == "capture" and output.output.HasField("local_path")
        capture = retained / name
        copytree(output.output.local_path, capture)
    else:
        assert not result.outputs
    client.ReportTestResult(
        pb.ReportTestResultRequest(result=pb.TestResult(target=spec.target.handle, name=label, status=status)),
        timeout=10,
    )
    return [label, pb.TestStatus.Name(status), str(capture) if capture else None]


def main():
    boundary = sys.argv.index("--")
    launch, args = sys.argv[1:boundary], sys.argv[boundary + 1:]
    parser = argparse.ArgumentParser()
    parser.add_argument("--executor-fd", type=int, required=True)
    parser.add_argument("--orchestrator-fd", type=int, required=True)
    fds, _ = parser.parse_known_args(launch)
    # Buck adds compatibility arguments before user executor arguments.
    retained = Path(args[-1])
    retained.mkdir()
    handler = Executor()
    with ThreadPoolExecutor(max_workers=2) as pool, socket.create_server(("127.0.0.1", 0)) as listener:
        server = grpc.server(pool)
        rpc.add_TestExecutorServicer_to_server(handler, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        # Python gRPC cannot adopt the SDK socketpair descriptors. Relay only
        # inside the fixture, to an ephemeral loopback gRPC endpoint.
        executor = socket.socket(fileno=fds.executor_fd)
        Thread(
            target=relay,
            args=(executor, socket.create_connection(("127.0.0.1", port))),
            daemon=True,
        ).start()
        orchestrator = socket.socket(fileno=fds.orchestrator_fd)
        Thread(target=lambda: relay(listener.accept()[0], orchestrator), daemon=True).start()
        try:
            with grpc.insecure_channel(f"127.0.0.1:{listener.getsockname()[1]}") as channel:
                client = rpc.TestOrchestratorStub(channel)
                rows = [
                    run_test(client, spec, retained)
                    for spec in iter(lambda: handler.requests.get(timeout=30), None)
                ]
                (retained / "results.json").write_text(json.dumps(sorted(rows)))
                client.EndOfTestResults(
                    pb.EndOfTestResultsRequest(exit_code=32 if any(row[1] == "FAIL" for row in rows) else 0),
                    timeout=10,
                )
        finally:
            server.stop(0).wait()


if __name__ == "__main__":
    main()
