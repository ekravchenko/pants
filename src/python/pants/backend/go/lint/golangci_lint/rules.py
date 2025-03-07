# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import textwrap
from dataclasses import dataclass

from pants.backend.go.lint.golangci_lint.skip_field import SkipGolangciLintField
from pants.backend.go.lint.golangci_lint.subsystem import GolangciLint
from pants.backend.go.subsystems.golang import GolangSubsystem
from pants.backend.go.target_types import GoPackageSourcesField
from pants.backend.go.util_rules.go_mod import (
    GoModInfo,
    GoModInfoRequest,
    OwningGoMod,
    OwningGoModRequest,
)
from pants.backend.go.util_rules.goroot import GoRoot
from pants.core.goals.lint import LintResult, LintTargetsRequest, Partitions
from pants.core.util_rules.config_files import ConfigFiles, ConfigFilesRequest
from pants.core.util_rules.external_tool import DownloadedExternalTool, ExternalToolRequest
from pants.core.util_rules.source_files import SourceFiles, SourceFilesRequest
from pants.core.util_rules.system_binaries import BashBinary
from pants.engine.fs import CreateDigest, Digest, FileContent, MergeDigests
from pants.engine.internals.selectors import Get, MultiGet
from pants.engine.platform import Platform
from pants.engine.process import FallibleProcessResult, Process
from pants.engine.rules import collect_rules, rule
from pants.engine.target import (
    FieldSet,
    SourcesField,
    Target,
    TransitiveTargets,
    TransitiveTargetsRequest,
)
from pants.util.logging import LogLevel


@dataclass(frozen=True)
class GolangciLintFieldSet(FieldSet):
    required_fields = (GoPackageSourcesField,)

    sources: GoPackageSourcesField

    @classmethod
    def opt_out(cls, tgt: Target) -> bool:
        return tgt.get(SkipGolangciLintField).value


class GolangciLintRequest(LintTargetsRequest):
    field_set_type = GolangciLintFieldSet
    tool_name = GolangciLint.options_scope


@rule
async def partition_golangci_lint(
    request: GolangciLintRequest.PartitionRequest[GolangciLintFieldSet], golangci_lint: GolangciLint
) -> Partitions[GolangciLintFieldSet]:
    return Partitions() if golangci_lint.skip else Partitions.single_partition(request.field_sets)


@rule(desc="Lint with golangci-lint", level=LogLevel.DEBUG)
async def run_golangci_lint(
    request: GolangciLintRequest.SubPartition[GolangciLintFieldSet],
    golangci_lint: GolangciLint,
    goroot: GoRoot,
    bash: BashBinary,
    platform: Platform,
    golang_subsystem: GolangSubsystem,
) -> LintResult:
    transitive_targets = await Get(
        TransitiveTargets,
        TransitiveTargetsRequest((field_set.address for field_set in request.elements)),
    )

    all_source_files_request = Get(
        SourceFiles,
        SourceFilesRequest(
            tgt[SourcesField] for tgt in transitive_targets.closure if tgt.has_field(SourcesField)
        ),
    )

    target_source_files_request = Get(
        SourceFiles,
        SourceFilesRequest(field_set.sources for field_set in request.elements),
    )

    downloaded_golangci_lint_request = Get(
        DownloadedExternalTool,
        ExternalToolRequest,
        golangci_lint.get_request(platform),
    )

    config_files_request = Get(ConfigFiles, ConfigFilesRequest, golangci_lint.config_request())

    (
        target_source_files,
        all_source_files,
        downloaded_golangci_lint,
        config_files,
    ) = await MultiGet(
        target_source_files_request,
        all_source_files_request,
        downloaded_golangci_lint_request,
        config_files_request,
    )

    owning_go_mods = await MultiGet(
        Get(OwningGoMod, OwningGoModRequest(field_set.address)) for field_set in request.elements
    )

    owning_go_mod_addresses = {x.address for x in owning_go_mods}

    go_mod_infos = await MultiGet(
        Get(GoModInfo, GoModInfoRequest(address)) for address in owning_go_mod_addresses
    )

    # golangci-lint requires a absolute path to a cache
    golangci_lint_run_script = FileContent(
        "__run_golangci_lint.sh",
        textwrap.dedent(
            f"""\
            export GOROOT={goroot.path}
            sandbox_root="$(/bin/pwd)"
            export PATH="${{GOROOT}}/bin:${{PATH}}"
            export GOPATH="${{sandbox_root}})/gopath"
            export GOCACHE="${{sandbox_root}}/gocache"
            export GOLANGCI_LINT_CACHE="$GOCACHE"
            export CGO_ENABLED={1 if golang_subsystem.cgo_enabled else 0}
            /bin/mkdir -p "$GOPATH" "$GOCACHE"
            exec "$@"
            """
        ).encode("utf-8"),
    )

    golangci_lint_run_script_digest = await Get(Digest, CreateDigest([golangci_lint_run_script]))

    input_digest = await Get(
        Digest,
        MergeDigests(
            [
                golangci_lint_run_script_digest,
                downloaded_golangci_lint.digest,
                config_files.snapshot.digest,
                target_source_files.snapshot.digest,
                all_source_files.snapshot.digest,
                *(info.digest for info in set(go_mod_infos)),
            ]
        ),
    )

    argv = [
        bash.path,
        golangci_lint_run_script.path,
        downloaded_golangci_lint.exe,
        "run",
        # keep golangci-lint from complaining
        # about concurrent runs
        "--allow-parallel-runners",
    ]
    if golangci_lint.config:
        argv.append(f"--config={golangci_lint.config}")
    elif config_files.snapshot.files:
        argv.append(f"--config={config_files.snapshot.files[0]}")
    else:
        argv.append("--no-config")
    argv.extend(golangci_lint.args)

    process_result = await Get(
        FallibleProcessResult,
        Process(
            argv=argv,
            input_digest=input_digest,
            description="Run `golangci-lint`.",
            level=LogLevel.DEBUG,
        ),
    )

    return LintResult.from_fallible_process_result(
        process_result, linter_name=GolangciLint.options_scope
    )


def rules():
    return [
        *collect_rules(),
        *GolangciLintRequest.registration_rules(),
    ]
