# Copyright 2019 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from abc import ABCMeta, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Any, Iterable, Optional, Tuple, Type, TypeVar

import pytest

from pants.base.specs import Specs
from pants.core.goals.fmt import FmtFilesRequest, FmtTargetsRequest
from pants.core.goals.lint import (
    Lint,
    LintFilesRequest,
    LintRequest,
    LintResult,
    LintSubsystem,
    LintTargetsRequest,
    Partitions,
    lint,
)
from pants.core.util_rules.distdir import DistDir
from pants.engine.addresses import Address
from pants.engine.fs import PathGlobs, SpecsPaths, Workspace
from pants.engine.internals.native_engine import EMPTY_SNAPSHOT, Snapshot
from pants.engine.target import FieldSet, FilteredTargets, MultipleSourcesField, Target
from pants.engine.unions import UnionMembership
from pants.testutil.option_util import create_goal_subsystem
from pants.testutil.rule_runner import MockGet, RuleRunner, mock_console, run_rule_with_mocks
from pants.util.logging import LogLevel

_LintRequestT = TypeVar("_LintRequestT", bound=LintRequest)


class MockMultipleSourcesField(MultipleSourcesField):
    pass


class MockTarget(Target):
    alias = "mock_target"
    core_fields = (MockMultipleSourcesField,)


@dataclass(frozen=True)
class MockLinterFieldSet(FieldSet):
    required_fields = (MultipleSourcesField,)
    sources: MultipleSourcesField


class MockLintRequest(LintRequest, metaclass=ABCMeta):
    @staticmethod
    @abstractmethod
    def exit_code(_: Iterable[Address]) -> int:
        pass

    @classmethod
    @abstractmethod
    def get_lint_result(cls, elements: Iterable) -> LintResult:
        pass


class MockLintTargetsRequest(MockLintRequest, LintTargetsRequest):
    field_set_type = MockLinterFieldSet

    @classmethod
    def get_lint_result(cls, field_sets: Iterable[MockLinterFieldSet]) -> LintResult:
        addresses = [field_set.address for field_set in field_sets]
        return LintResult(cls.exit_code(addresses), "", "", cls.tool_name)


class SuccessfulRequest(MockLintTargetsRequest):
    tool_name = "SuccessfulLinter"

    @staticmethod
    def exit_code(_: Iterable[Address]) -> int:
        return 0


class FailingRequest(MockLintTargetsRequest):
    tool_name = "FailingLinter"

    @staticmethod
    def exit_code(_: Iterable[Address]) -> int:
        return 1


class ConditionallySucceedsRequest(MockLintTargetsRequest):
    tool_name = "ConditionallySucceedsLinter"

    @staticmethod
    def exit_code(addresses: Iterable[Address]) -> int:
        if any(address.target_name == "bad" for address in addresses):
            return 127
        return 0


class SkippedRequest(MockLintTargetsRequest):
    tool_name = "SkippedLinter"

    @staticmethod
    def exit_code(_) -> int:
        return 0


class InvalidField(MultipleSourcesField):
    pass


class InvalidFieldSet(MockLinterFieldSet):
    required_fields = (InvalidField,)


class InvalidRequest(MockLintTargetsRequest):
    field_set_type = InvalidFieldSet
    tool_name = "InvalidLinter"

    @staticmethod
    def exit_code(_: Iterable[Address]) -> int:
        return -1


def mock_target_partitioner(
    request: MockLintTargetsRequest.PartitionRequest,
) -> Partitions[MockLinterFieldSet]:
    if type(request) is SkippedRequest.PartitionRequest:
        return Partitions()

    if type(request) in {SuccessfulFormatter.PartitionRequest, FailingFormatter.PartitionRequest}:
        return Partitions.single_partition(fs.sources.globs for fs in request.field_sets)

    return Partitions.single_partition(request.field_sets)


class MockFilesRequest(MockLintRequest, LintFilesRequest):
    tool_name = "FilesLinter"

    @classmethod
    def get_lint_result(cls, files: Iterable[str]) -> LintResult:
        return LintResult(0, "", "", cls.tool_name)


def mock_file_partitioner(request: MockFilesRequest.PartitionRequest) -> Partitions[str]:
    return Partitions.single_partition(request.files)


def _all_lint_requests() -> Iterable[type[MockLintRequest]]:
    classes = [MockLintRequest]
    while classes:
        cls = classes.pop()
        subclasses = cls.__subclasses__()
        classes.extend(subclasses)
        yield from subclasses


def mock_lint_partition(request: Any) -> LintResult:
    request_type = {cls.SubPartition: cls for cls in _all_lint_requests()}[type(request)]
    return request_type.get_lint_result(request.elements)


class MockFmtRequest(MockLintRequest, FmtTargetsRequest):
    field_set_type = MockLinterFieldSet


class SuccessfulFormatter(MockFmtRequest):
    tool_name = "SuccessfulFormatter"

    @classmethod
    def get_lint_result(cls, field_sets: Iterable[MockLinterFieldSet]) -> LintResult:
        return LintResult(0, "", "", cls.tool_name)


class FailingFormatter(MockFmtRequest):
    tool_name = "FailingFormatter"

    @classmethod
    def get_lint_result(cls, field_sets: Iterable[MockLinterFieldSet]) -> LintResult:
        return LintResult(1, "", "", cls.tool_name)


class BuildFileFormatter(MockLintRequest, FmtFilesRequest):
    tool_name = "BobTheBUILDer"

    @classmethod
    def get_lint_result(cls, files: Iterable[str]) -> LintResult:
        return LintResult(0, "", "", cls.tool_name)


@pytest.fixture
def rule_runner() -> RuleRunner:
    return RuleRunner()


def make_target(address: Optional[Address] = None) -> Target:
    return MockTarget({}, address or Address("", target_name="tests"))


def run_lint_rule(
    rule_runner: RuleRunner,
    *,
    lint_request_types: Iterable[Type[_LintRequestT]],
    targets: list[Target],
    batch_size: int = 128,
    only: list[str] | None = None,
    skip_formatters: bool = False,
) -> Tuple[int, str]:
    union_membership = UnionMembership(
        {
            LintRequest: lint_request_types,
            LintRequest.SubPartition: [rt.SubPartition for rt in lint_request_types],
            LintTargetsRequest.PartitionRequest: [
                rt.PartitionRequest
                for rt in lint_request_types
                if issubclass(rt, LintTargetsRequest)
            ],
            LintFilesRequest.PartitionRequest: [
                rt.PartitionRequest for rt in lint_request_types if issubclass(rt, LintFilesRequest)
            ],
        }
    )
    lint_subsystem = create_goal_subsystem(
        LintSubsystem,
        batch_size=batch_size,
        only=only or [],
        skip_formatters=skip_formatters,
    )
    with mock_console(rule_runner.options_bootstrapper) as (console, stdio_reader):
        result: Lint = run_rule_with_mocks(
            lint,
            rule_args=[
                console,
                Workspace(rule_runner.scheduler, _enforce_effects=False),
                Specs.empty(),
                lint_subsystem,
                union_membership,
                DistDir(relpath=Path("dist")),
            ],
            mock_gets=[
                MockGet(
                    output_type=Partitions,
                    input_types=(LintTargetsRequest.PartitionRequest,),
                    mock=mock_target_partitioner,
                ),
                MockGet(
                    output_type=Partitions,
                    input_types=(LintFilesRequest.PartitionRequest,),
                    mock=mock_file_partitioner,
                ),
                MockGet(
                    output_type=LintResult,
                    input_types=(LintRequest.SubPartition,),
                    mock=mock_lint_partition,
                ),
                MockGet(
                    output_type=FilteredTargets,
                    input_types=(Specs,),
                    mock=lambda _: FilteredTargets(tuple(targets)),
                ),
                MockGet(
                    output_type=SpecsPaths,
                    input_types=(Specs,),
                    mock=lambda _: SpecsPaths(("f.txt", "BUILD"), ()),
                ),
                MockGet(
                    output_type=Snapshot,
                    input_types=(PathGlobs,),
                    mock=lambda _: EMPTY_SNAPSHOT,
                ),
            ],
            union_membership=union_membership,
        )
        assert not stdio_reader.get_stdout()
        return result.exit_code, stdio_reader.get_stderr()


def test_invalid_target_noops(rule_runner: RuleRunner) -> None:
    exit_code, stderr = run_lint_rule(
        rule_runner, lint_request_types=[InvalidRequest], targets=[make_target()]
    )
    assert exit_code == 0
    assert stderr == ""


def test_summary(rule_runner: RuleRunner) -> None:
    """Test that we render the summary correctly.

    This tests that we:
    * Merge multiple results belonging to the same linter (`--per-file-caching`).
    * Decide correctly between skipped, failed, and succeeded.
    """
    good_address = Address("", target_name="good")
    bad_address = Address("", target_name="bad")

    request_types = [
        ConditionallySucceedsRequest,
        FailingRequest,
        SkippedRequest,
        SuccessfulRequest,
        SuccessfulFormatter,
        FailingFormatter,
        BuildFileFormatter,
        MockFilesRequest,
    ]
    targets = [make_target(good_address), make_target(bad_address)]

    exit_code, stderr = run_lint_rule(
        rule_runner,
        lint_request_types=request_types,
        targets=targets,
    )
    assert exit_code == FailingRequest.exit_code([bad_address])
    assert stderr == dedent(
        """\

        ✓ BobTheBUILDer succeeded.
        ✕ ConditionallySucceedsLinter failed.
        ✕ FailingFormatter failed.
        ✕ FailingLinter failed.
        ✓ FilesLinter succeeded.
        ✓ SuccessfulFormatter succeeded.
        ✓ SuccessfulLinter succeeded.

        (One or more formatters failed. Run `./pants fmt` to fix.)
        """
    )

    exit_code, stderr = run_lint_rule(
        rule_runner,
        lint_request_types=request_types,
        targets=targets,
        only=[
            FailingRequest.tool_name,
            MockFilesRequest.tool_name,
            FailingFormatter.tool_name,
            BuildFileFormatter.tool_name,
        ],
    )
    assert stderr == dedent(
        """\

        ✓ BobTheBUILDer succeeded.
        ✕ FailingFormatter failed.
        ✕ FailingLinter failed.
        ✓ FilesLinter succeeded.

        (One or more formatters failed. Run `./pants fmt` to fix.)
        """
    )

    exit_code, stderr = run_lint_rule(
        rule_runner,
        lint_request_types=request_types,
        targets=targets,
        skip_formatters=True,
    )
    assert stderr == dedent(
        """\

        ✕ ConditionallySucceedsLinter failed.
        ✕ FailingLinter failed.
        ✓ FilesLinter succeeded.
        ✓ SuccessfulLinter succeeded.
        """
    )


@pytest.mark.parametrize("batch_size", [1, 32, 128, 1024])
def test_batched(rule_runner: RuleRunner, batch_size: int) -> None:
    exit_code, stderr = run_lint_rule(
        rule_runner,
        lint_request_types=[
            ConditionallySucceedsRequest,
            FailingRequest,
            SkippedRequest,
            SuccessfulRequest,
        ],
        targets=[make_target(Address("", target_name=f"good{i}")) for i in range(0, 512)],
        batch_size=batch_size,
    )
    assert exit_code == FailingRequest.exit_code([])
    assert stderr == dedent(
        """\

        ✓ ConditionallySucceedsLinter succeeded.
        ✕ FailingLinter failed.
        ✓ SuccessfulLinter succeeded.
        """
    )


def test_streaming_output_success() -> None:
    result = LintResult(0, "stdout", "stderr", linter_name="linter")
    assert result.level() == LogLevel.INFO
    assert result.message() == dedent(
        """\
        linter succeeded.
        stdout
        stderr

        """
    )


def test_streaming_output_failure() -> None:
    result = LintResult(18, "stdout", "stderr", linter_name="linter")
    assert result.level() == LogLevel.ERROR
    assert result.message() == dedent(
        """\
        linter failed (exit code 18).
        stdout
        stderr

        """
    )


def test_streaming_output_partitions() -> None:
    result = LintResult(
        21, "stdout", "stderr", linter_name="linter", partition_description="ghc9.2"
    )
    assert result.level() == LogLevel.ERROR
    assert result.message() == dedent(
        """\
        linter failed (exit code 21).
        Partition: ghc9.2
        stdout
        stderr

        """
    )
