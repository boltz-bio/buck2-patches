# Buck2 patches

[![Build](https://github.com/boltz-bio/buck2-patches/actions/workflows/build.yaml/badge.svg)](https://github.com/boltz-bio/buck2-patches/actions/workflows/build.yaml)

[Buck2](https://github.com/meta/buck2) at a pinned upstream commit, plus the patches that make it publish its builds to a Bazel Build Event Service (BES) such as BuildBuddy's. Upstream Buck2 has no BES client, so its builds never appear as BuildBuddy invocations. Most of the patches are Mercury's open upstream pull requests, kept here until upstream merges them; one is ours.

Each release carries release `buck2` binaries for Linux x86_64 and macOS arm64, zstd-compressed and named as upstream names them, their sha256s, and the standard prelude from the same upstream commit, which the binaries also bundle.

## What's patched

`patches/buck2/version.txt` pins the upstream `meta/buck2` commit. `patches/buck2/*.patch` apply to it in order, with `git am`:

| Patch | Upstream | What it does |
| :-- | :-- | :-- |
| `0001-pr1597-bes-and-bep-protocol-crates.patch` | [meta/buck2#1597](https://github.com/meta/buck2/pull/1597) (open) | Adds the Build Event Service and Bazel Build Event Protocol protocol crates under `remote_execution/oss`. |
| `0002-pr1598-action-names-in-re-metadata.patch` | [meta/buck2#1598](https://github.com/meta/buck2/pull/1598) (open) | Names actions in remote execution request metadata, and records the data a BEP stream needs. |
| `0003-pr1599-publish-to-bazel-bes.patch` | [meta/buck2#1599](https://github.com/meta/buck2/pull/1599) (open) | Publishes the event stream to a Bazel Build Event Service in open-source builds, configured by the `[bes]` buckconfig section (`docs/users/build_observability/event_system.md`). `event_format = bazel` converts it to Bazel BEP, which BuildBuddy renders. |
| `0004-pr1600-kill-waits-for-bes-ack.patch` | [meta/buck2#1600](https://github.com/meta/buck2/pull/1600) (open) | Lets `buck2 kill` wait for the BES server to acknowledge the daemon's events (`--bes-drain-timeout`), so a CI job that kills its daemon doesn't lose the end of the stream. |
| `0005-bes-example-check.patch` | Mercury's `bes-upstream/5-bes-ci` branch, not an open pull request | Adds `examples/bes`, which runs buck2 against a fake BES server and checks what it receives. It changes no code in the binary; CI runs it on the Linux binary. |
| `0006-bes-metadata-and-start-deadline.patch` | Ours, not yet sent upstream | Each `--client-metadata bes_<key>=<value>` (or `BUCK2_CLIENT_METADATA` entry) becomes `<KEY>=<value>` in the command's Bazel `BuildMetadata`, so `bes_commit_sha=…` sets BuildBuddy's `COMMIT_SHA`: `[bes] build_metadata` is read once, when the daemon starts, so it can't carry a commit or branch for a daemon that outlives one CI run, as a resumed runner's does, and client metadata keys must be snake_case. `BUCK2_BES_COMMAND_START_DEADLINE_MS` in the daemon's environment shortens the 2 s each command waits for its `CommandStart` to be sent, which an unreachable server adds to every command. |

Patches 0001 to 0005 are `git format-patch` of Mercury's commits, unchanged, from the heads of its stacked pull requests (#1598's head contains #1597's commit, and so on; `bes-upstream/5-bes-ci` holds all five).

The pin is `2920a4c3bc` (2026-11-06), the parent of [`b673d4d04a`](https://github.com/meta/buck2/commit/b673d4d04a), "types: The test info providers hold `ValueOf`". The patches still apply after it, but patch 0002's `test_labels` in `app/buck2_test/src/orchestrator.rs` no longer compiles there: `labels()` now yields `Result`s. Bump past it once Mercury rebases #1598, or adapt 0002 here.

Things to know when configuring it:

- The daemon reads `[bes]` from the root cell's `.buckconfig` files when it starts, not from `--config-file` or `-c`, and expands `$VAR` in `header` and `build_metadata` from its own environment then.
- A `header` whose variable is unset or empty turns BES off for that daemon, so an API key in the environment can switch it on and off.
- With `event_format = bazel`, every invocation gets `VISIBILITY=PUBLIC` in its BuildMetadata unless `build_metadata` or the command sets `VISIBILITY`, which makes it public on BuildBuddy. Set it.

[meta/buck2#1608](https://github.com/meta/buck2/pull/1608) (shim fix-ups) isn't carried: it fixes the bootstrap build with Buck2 itself (`buck2 build //:buck2`) on macOS and Windows, and the release builds here use Cargo, as upstream's do.

## How it builds

`.github/workflows/build.yaml` runs on every push and pull request:

1. `scripts/apply.sh` checks out the pin and applies the patches. A patch that doesn't apply fails the run and names the patch.
2. It builds `buck2` with upstream's release flags (`cargo build --release`, `-C codegen-units=1`, the toolchain from `rust-toolchain.toml`) on `ubuntu-26.04` and `macos-15`.
3. It checks each binary on `examples/with_prelude`, as upstream's release workflow does, and on Linux runs `examples/bes/check.py` and checks that the bundled prelude matches the tree's.

To try the patches locally:

```sh
scripts/apply.sh /tmp/buck2
cd /tmp/buck2 && cargo build --release --bin buck2
```

## Releasing

Releases are tagged `<upstream date>-bes.<n>`: the UTC commit date of the pin, then a counter that starts at 1 for each pin. Pushing the tag builds and publishes the release:

```sh
git tag 2026-11-06-bes.1 && git push origin 2026-11-06-bes.1
```

The workflow refuses a tag whose date isn't the pin's. The release holds:

- `buck2-x86_64-unknown-linux-gnu.zst` and `buck2-aarch64-apple-darwin.zst`;
- `prelude.tar.zst`, the pinned commit's `prelude/`, and `prelude_hash`, that commit;
- a `.sha256` beside each file, and `SHA256SUMS`.

`buck2 --version` prints `<tag>-<pinned commit>`.

## Bumping the pin

1. Pick an upstream `main` commit, newer than the pin, and write it to `patches/buck2/version.txt`.
2. Run `scripts/apply.sh /tmp/buck2`. If a patch no longer applies, rebase it: apply the ones before it, `git am --3way` it, resolve, commit, and export the series again with `git format-patch --no-signature --zero-commit -o patches/buck2 <pin>..HEAD`, keeping the file names.
3. Open a pull request. When CI is green, merge it and tag `<new date>-bes.1`.

## Dropping a patch

Once upstream merges a pull request, bump the pin to a commit that contains it, delete its patch, and renumber nothing: the numbers only order the files. If a later patch was rebased on the merged one, check that the rest still apply (step 2 above). Update the table above.

## Licence

The patches are contributions to Buck2 and carry its licence, MIT or Apache 2.0 at your option (`LICENSE-MIT`, `LICENSE-APACHE`).
