# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from dataclasses import dataclass

from pants.backend.codegen.protobuf.lint.buf.skip_field import SkipBufLintField
from pants.backend.codegen.protobuf.lint.buf.subsystem import BufSubsystem
from pants.backend.codegen.protobuf.target_types import (
    ProtobufDependenciesField,
    ProtobufSourceField,
)
from pants.core.goals.lint import LintResult, LintTargetsRequest, Partitions
from pants.core.util_rules.external_tool import DownloadedExternalTool, ExternalToolRequest
from pants.core.util_rules.source_files import SourceFilesRequest
from pants.core.util_rules.stripped_source_files import StrippedSourceFiles
from pants.engine.fs import Digest, MergeDigests
from pants.engine.platform import Platform
from pants.engine.process import FallibleProcessResult, Process
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import FieldSet, Target, TransitiveTargets, TransitiveTargetsRequest
from pants.util.logging import LogLevel
from pants.util.strutil import pluralize


@dataclass(frozen=True)
class BufFieldSet(FieldSet):
    required_fields = (ProtobufSourceField,)

    sources: ProtobufSourceField
    dependencies: ProtobufDependenciesField

    @classmethod
    def opt_out(cls, tgt: Target) -> bool:
        return tgt.get(SkipBufLintField).value


class BufLintRequest(LintTargetsRequest):
    field_set_type = BufFieldSet
    tool_name = "buf-lint"


@rule
async def partition_buf(
    request: BufLintRequest.PartitionRequest[BufFieldSet], buf: BufSubsystem
) -> Partitions[BufFieldSet]:
    return Partitions() if buf.lint_skip else Partitions.single_partition(request.field_sets)


@rule(desc="Lint with buf lint", level=LogLevel.DEBUG)
async def run_buf(
    request: BufLintRequest.SubPartition[BufFieldSet], buf: BufSubsystem, platform: Platform
) -> LintResult:
    transitive_targets = await Get(
        TransitiveTargets,
        TransitiveTargetsRequest((field_set.address for field_set in request.elements)),
    )

    all_stripped_sources_request = Get(
        StrippedSourceFiles,
        SourceFilesRequest(
            tgt[ProtobufSourceField]
            for tgt in transitive_targets.closure
            if tgt.has_field(ProtobufSourceField)
        ),
    )
    target_stripped_sources_request = Get(
        StrippedSourceFiles,
        SourceFilesRequest(
            (field_set.sources for field_set in request.elements),
            for_sources_types=(ProtobufSourceField,),
            enable_codegen=True,
        ),
    )

    download_buf_get = Get(DownloadedExternalTool, ExternalToolRequest, buf.get_request(platform))

    target_sources_stripped, all_sources_stripped, downloaded_buf = await MultiGet(
        target_stripped_sources_request, all_stripped_sources_request, download_buf_get
    )

    input_digest = await Get(
        Digest,
        MergeDigests(
            (
                target_sources_stripped.snapshot.digest,
                all_sources_stripped.snapshot.digest,
                downloaded_buf.digest,
            )
        ),
    )

    process_result = await Get(
        FallibleProcessResult,
        Process(
            argv=[
                downloaded_buf.exe,
                "lint",
                *buf.lint_args,
                "--path",
                ",".join(target_sources_stripped.snapshot.files),
            ],
            input_digest=input_digest,
            description=f"Run buf lint on {pluralize(len(request.elements), 'file')}.",
            level=LogLevel.DEBUG,
        ),
    )
    return LintResult.from_fallible_process_result(
        process_result, linter_name=BufLintRequest.tool_name
    )


def rules():
    return [*collect_rules(), *BufLintRequest.registration_rules()]
