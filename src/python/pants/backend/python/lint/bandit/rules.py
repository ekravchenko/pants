# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from typing import Tuple, cast

from pants.backend.python.lint.bandit.subsystem import Bandit, BanditFieldSet
from pants.backend.python.subsystems.setup import PythonSetup
from pants.backend.python.util_rules import pex
from pants.backend.python.util_rules.interpreter_constraints import InterpreterConstraints
from pants.backend.python.util_rules.pex import PexRequest, VenvPex, VenvPexProcess
from pants.core.goals.lint import REPORT_DIR, LintResult, LintTargetsRequest, Partitions
from pants.core.util_rules.config_files import ConfigFiles, ConfigFilesRequest
from pants.core.util_rules.source_files import SourceFiles, SourceFilesRequest
from pants.engine.fs import CreateDigest, Digest, Directory, MergeDigests, RemovePrefix
from pants.engine.process import FallibleProcessResult
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.util.logging import LogLevel
from pants.util.strutil import pluralize


class BanditRequest(LintTargetsRequest):
    field_set_type = BanditFieldSet
    tool_name = Bandit.options_scope


def generate_argv(source_files: SourceFiles, bandit: Bandit) -> Tuple[str, ...]:
    args = []
    if bandit.config is not None:
        args.append(f"--config={bandit.config}")
    args.extend(bandit.args)
    args.extend(source_files.files)
    return tuple(args)


@rule
async def partition_bandit(
    request: BanditRequest.PartitionRequest[BanditFieldSet],
    bandit: Bandit,
    python_setup: PythonSetup,
) -> Partitions[BanditFieldSet]:
    if bandit.skip:
        return Partitions()

    # NB: Bandit output depends upon which Python interpreter version it's run with
    # ( https://github.com/PyCQA/bandit#under-which-version-of-python-should-i-install-bandit).
    # We batch targets by their constraints to ensure, for example, that all Python 2 targets run
    # together and all Python 3 targets run together.
    constraints_to_field_sets = InterpreterConstraints.group_field_sets_by_constraints(
        request.field_sets, python_setup
    )

    return Partitions(constraints_to_field_sets)


@rule(desc="Lint with Bandit", level=LogLevel.DEBUG)
async def bandit_lint(
    request: BanditRequest.SubPartition[BanditFieldSet], bandit: Bandit
) -> LintResult:
    interpreter_constraints = cast(InterpreterConstraints, request.key)
    bandit_pex_get = Get(
        VenvPex,
        PexRequest,
        bandit.to_pex_request(interpreter_constraints=interpreter_constraints),
    )

    config_files_get = Get(ConfigFiles, ConfigFilesRequest, bandit.config_request)
    source_files_get = Get(
        SourceFiles, SourceFilesRequest(field_set.source for field_set in request.elements)
    )
    # Ensure that the empty report dir exists.
    report_directory_digest_get = Get(Digest, CreateDigest([Directory(REPORT_DIR)]))

    bandit_pex, config_files, report_directory, source_files = await MultiGet(
        bandit_pex_get, config_files_get, report_directory_digest_get, source_files_get
    )

    input_digest = await Get(
        Digest,
        MergeDigests(
            (source_files.snapshot.digest, config_files.snapshot.digest, report_directory)
        ),
    )

    result = await Get(
        FallibleProcessResult,
        VenvPexProcess(
            bandit_pex,
            argv=generate_argv(source_files, bandit),
            input_digest=input_digest,
            description=f"Run Bandit on {pluralize(len(request.elements), 'file')}.",
            output_directories=(REPORT_DIR,),
            level=LogLevel.DEBUG,
        ),
    )
    report = await Get(Digest, RemovePrefix(result.output_digest, REPORT_DIR))
    return LintResult.from_fallible_process_result(
        result,
        partition_description=str(sorted(str(c) for c in interpreter_constraints)),
        linter_name=Bandit.options_scope,
        report=report,
    )


def rules():
    return [*collect_rules(), *BanditRequest.registration_rules(), *pex.rules()]
