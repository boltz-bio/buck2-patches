#!/usr/bin/env python3
"""Check physical source path rendering and path-only caching on a real Buck2 binary."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import tempfile

RULES = """
def _export(ctx):
    return [DefaultInfo(default_output = ctx.attrs.src)]

export = rule(impl = _export, attrs = {"src": attrs.source(allow_directory = True)})

def _relay(ctx):
    return [ctx.attrs.src[DefaultInfo]]

relay = rule(impl = _relay, attrs = {"src": attrs.dep()})

def _contract(ctx):
    own = ctx.attrs.own
    foreign = ctx.attrs.foreign
    relayed = ctx.attrs.relay[DefaultInfo].default_outputs[0]
    directory = ctx.attrs.directory
    projected = directory.project("nested").project("file.txt", hide_prefix = True)
    if projected.short_path != "file.txt":
        fail("Projection did not hide its short-path prefix")
    paths = ctx.actions.write_json("paths.json", {
        "physical": cmd_args([own, foreign, relayed, projected], relative_to = ctx.label.project_root, ignore_artifacts = True),
    })
    result = ctx.actions.declare_output("paths.copy.json")
    ctx.actions.run(
        cmd_args("/bin/sh", "-c", 'cat "$1" > "$2"', "source-path-contract", paths, result.as_output()),
        category = "source_path_contract",
    )
    return [DefaultInfo(default_output = result)]

contract = rule(impl = _contract, attrs = {
    "own": attrs.source(),
    "foreign": attrs.source(),
    "relay": attrs.dep(),
    "directory": attrs.source(allow_directory = True),
})
"""

ROOT_BUILD = """load(":defs.bzl", "contract")
contract(
    name = "contract",
    own = "//local:root.txt",
    foreign = "foreign_alias//origin:source",
    relay = "//relay:source",
    directory = "foreign_alias//origin:tree",
)
"""


def run(binary: pathlib.Path, project: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        [str(binary), *args], cwd=project, capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError(
            f"Buck2 {' '.join(args)} failed ({result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def build(
    binary: pathlib.Path, project: pathlib.Path
) -> dict[str, list[str]]:
    outputs = json.loads(
        run(binary, project, "build", "//:contract", "--show-full-json-output")
    )
    output = outputs["root//:contract"]
    return json.loads(pathlib.Path(output).read_text())


def executions(binary: pathlib.Path, project: pathlib.Path) -> list[str]:
    records = []
    for line in run(
        binary, project, "log", "what-ran", "--format", "json"
    ).splitlines():
        item = json.loads(line)
        if "source_path_contract" in str(item.get("identity", "")):
            executor = item.get("reproducer", {}).get("executor", "")
            if executor != "Cache":
                records.append(executor)
    return records


def check(binary: pathlib.Path, project: pathlib.Path) -> None:
    (project / "foreign/origin/tree/nested").mkdir(parents=True)
    (project / "local").mkdir()
    (project / "relay").mkdir()
    (project / ".buckconfig").write_text(
        "[cells]\nroot = .\nother = foreign\n"
        "[cell_aliases]\nforeign_alias = other\n[buildfile]\nname = BUCK\n"
    )
    (project / "defs.bzl").write_text(RULES)
    (project / "local/root.txt").write_text("root contents")
    (project / "local/BUCK").write_text(
        'load("//:defs.bzl", "export")\nexport(name = "root.txt", src = "root.txt", visibility = ["PUBLIC"])\n'
    )
    (project / "foreign/origin/tree/nested/file.txt").write_text("projected contents")
    source = project / "foreign/origin/source.txt"
    source.write_text("original contents")
    foreign_build = project / "foreign/origin/BUCK"
    foreign_build.write_text(
        'load("@root//:defs.bzl", "export")\n'
        'export(name = "source", src = "source.txt", visibility = ["PUBLIC"])\n'
        'export(name = "tree", src = "tree", visibility = ["PUBLIC"])\n'
    )
    (project / "relay/BUCK").write_text(
        'load("//:defs.bzl", "relay")\n'
        'relay(name = "source", src = "foreign_alias//origin:source", visibility = ["PUBLIC"])\n'
    )
    (project / "BUCK").write_text(ROOT_BUILD)
    before = build(binary, project)
    assert before["physical"] == [
        "local/root.txt",
        "foreign/origin/source.txt",
        "foreign/origin/source.txt",
        "foreign/origin/tree/nested/file.txt",
    ]
    assert executions(binary, project), (
        "The downstream path consumer did not execute initially"
    )
    source.write_text("changed contents with the same path")
    assert build(binary, project) == before, (
        "A content edit changed source path metadata"
    )
    assert not executions(binary, project), (
        "A source content edit reran the path-only consumer"
    )

    source.rename(source.with_name("renamed.txt"))
    foreign_build.write_text(
        foreign_build.read_text().replace('src = "source.txt"', 'src = "renamed.txt"')
    )
    after = build(binary, project)
    assert after != before, "A source rename did not change source path metadata"
    assert executions(binary, project), (
        "A source rename did not rerun the path-only consumer"
    )
    assert after["physical"] == [
        "local/root.txt",
        "foreign/origin/renamed.txt",
        "foreign/origin/renamed.txt",
        "foreign/origin/tree/nested/file.txt",
    ]
    print("PASS: physical paths for local files, cross-cell reexports and projections, and path-only caching")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buck2", required=True, type=pathlib.Path)
    args = parser.parse_args()
    binary = args.buck2.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="buck2-source-path-contract-") as folder:
        project = pathlib.Path(folder)
        try:
            check(binary, project)
        finally:
            subprocess.run(
                [str(binary), "kill"], cwd=project, capture_output=True, check=False
            )


if __name__ == "__main__":
    main()
