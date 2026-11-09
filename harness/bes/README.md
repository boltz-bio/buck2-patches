# Build Event Service check

`check.py` runs buck2 against `fake_bes_server.py`, a Build Event Service that
records every event it receives, and checks what arrived:

- a build that succeeds, one that fails, and a test run each stream a complete
  invocation, from `started` to `build_finished` and the end of the stream,
  with the right exit code and test statuses;
- a build of a target that declares no actions completes that target, and an
  analysis-only BXL script completes none;
- only the targets a build or test requests get target events: a dependency,
  built or rebuilt in a new daemon, gets none, a requested target that `buck2 test` does not
  build closes as skipped, a target named last among more patterns than fit
  in 512 KiB still counts as requested, and a target built in more than one
  configuration is announced once with a completion for each;
- `buck2 kill --bes-drain-timeout` delivers a stream that the daemon still held
  when its command ended, to a server that reads slowly and acknowledges only
  at the end of a stream.

```sh
python3 -m venv /tmp/bes-venv
/tmp/bes-venv/bin/pip install grpcio==1.76.0 grpcio-tools==1.76.0
/tmp/bes-venv/bin/python harness/bes/check.py --buck2 <path to buck2> --buck2-src <patched buck2 checkout>
```

The script generates the Python stubs from the protos under the checkout's
`remote_execution/oss` and writes this project's `.buckconfig.local`, which it
removes when the checks pass. The Build workflow runs it on the Linux binary.
