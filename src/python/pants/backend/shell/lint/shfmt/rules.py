# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from dataclasses import dataclass

from pants.backend.shell.lint.shfmt.skip_field import SkipShfmtField
from pants.backend.shell.lint.shfmt.subsystem import Shfmt
from pants.backend.shell.target_types import ShellSourceField
from pants.core.goals.fmt import FmtResult, FmtTargetsRequest, Partitions
from pants.core.util_rules.config_files import ConfigFiles, ConfigFilesRequest
from pants.core.util_rules.external_tool import DownloadedExternalTool, ExternalToolRequest
from pants.engine.fs import Digest, MergeDigests
from pants.engine.internals.native_engine import Snapshot
from pants.engine.platform import Platform
from pants.engine.process import Process, ProcessResult
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import FieldSet, Target
from pants.util.logging import LogLevel
from pants.util.strutil import pluralize


@dataclass(frozen=True)
class ShfmtFieldSet(FieldSet):
    required_fields = (ShellSourceField,)

    sources: ShellSourceField

    @classmethod
    def opt_out(cls, tgt: Target) -> bool:
        return tgt.get(SkipShfmtField).value


class ShfmtRequest(FmtTargetsRequest):
    field_set_type = ShfmtFieldSet
    tool_name = Shfmt.options_scope


@rule
async def partition_shfmt(request: ShfmtRequest.PartitionRequest, shfmt: Shfmt) -> Partitions:
    return (
        Partitions()
        if shfmt.skip
        else Partitions.single_partition(
            field_set.sources.file_path for field_set in request.field_sets
        )
    )


@rule(desc="Format with shfmt", level=LogLevel.DEBUG)
async def shfmt_fmt(
    request: ShfmtRequest.SubPartition, shfmt: Shfmt, platform: Platform
) -> FmtResult:
    download_shfmt_get = Get(
        DownloadedExternalTool, ExternalToolRequest, shfmt.get_request(platform)
    )
    config_files_get = Get(
        ConfigFiles, ConfigFilesRequest, shfmt.config_request(request.snapshot.dirs)
    )
    downloaded_shfmt, config_files = await MultiGet(download_shfmt_get, config_files_get)

    input_digest = await Get(
        Digest,
        MergeDigests(
            (request.snapshot.digest, downloaded_shfmt.digest, config_files.snapshot.digest)
        ),
    )

    argv = [
        downloaded_shfmt.exe,
        "-l",
        "-w",
        *shfmt.args,
        *request.files,
    ]

    result = await Get(
        ProcessResult,
        Process(
            argv=argv,
            input_digest=input_digest,
            output_files=request.files,
            description=f"Run shfmt on {pluralize(len(request.files), 'file')}.",
            level=LogLevel.DEBUG,
        ),
    )
    output_snapshot = await Get(Snapshot, Digest, result.output_digest)
    return FmtResult.create(
        result, request.snapshot, output_snapshot, formatter_name=ShfmtRequest.tool_name
    )


def rules():
    return [
        *collect_rules(),
        *ShfmtRequest.registration_rules(),
    ]
