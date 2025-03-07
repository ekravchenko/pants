# Copyright 2019 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    ClassVar,
    Generic,
    Iterable,
    Iterator,
    Sequence,
    Tuple,
    TypeVar,
    cast,
)

from typing_extensions import final

from pants.base.specs import Specs
from pants.core.goals.multi_tool_goal_helper import (
    BatchSizeOption,
    OnlyOption,
    determine_specified_tool_names,
    write_reports,
)
from pants.core.util_rules.distdir import DistDir
from pants.engine.console import Console
from pants.engine.engine_aware import EngineAwareParameter, EngineAwareReturnType
from pants.engine.environment import EnvironmentName
from pants.engine.fs import EMPTY_DIGEST, Digest, PathGlobs, SpecsPaths, Workspace
from pants.engine.goal import Goal, GoalSubsystem
from pants.engine.internals.native_engine import Snapshot
from pants.engine.process import FallibleProcessResult
from pants.engine.rules import Get, MultiGet, collect_rules, goal_rule, rule_helper
from pants.engine.target import FieldSet, FilteredTargets
from pants.engine.unions import UnionMembership, UnionRule, distinct_union_type_per_subclass, union
from pants.option.option_types import BoolOption
from pants.util.collections import partition_sequentially
from pants.util.docutil import bin_name
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel
from pants.util.meta import frozen_after_init, runtime_ignore_subscripts
from pants.util.strutil import softwrap, strip_v2_chroot_path

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_FieldSetT = TypeVar("_FieldSetT", bound=FieldSet)
_PartitionElementT = TypeVar("_PartitionElementT")


@dataclass(frozen=True)
class LintResult(EngineAwareReturnType):
    exit_code: int
    stdout: str
    stderr: str
    linter_name: str
    partition_description: str | None = None
    report: Digest = EMPTY_DIGEST

    @classmethod
    def from_fallible_process_result(
        cls,
        process_result: FallibleProcessResult,
        *,
        linter_name: str,
        partition_description: str | None = None,
        strip_chroot_path: bool = False,
        report: Digest = EMPTY_DIGEST,
    ) -> LintResult:
        def prep_output(s: bytes) -> str:
            return strip_v2_chroot_path(s) if strip_chroot_path else s.decode()

        return cls(
            exit_code=process_result.exit_code,
            stdout=prep_output(process_result.stdout),
            stderr=prep_output(process_result.stderr),
            linter_name=linter_name,
            partition_description=partition_description,
            report=report,
        )

    def metadata(self) -> dict[str, Any]:
        return {"partition": self.partition_description}

    def level(self) -> LogLevel | None:
        return LogLevel.ERROR if self.exit_code != 0 else LogLevel.INFO

    def message(self) -> str | None:
        message = self.linter_name
        message += (
            " succeeded." if self.exit_code == 0 else f" failed (exit code {self.exit_code})."
        )
        if self.partition_description:
            message += f"\nPartition: {self.partition_description}"
        if self.stdout:
            message += f"\n{self.stdout}"
        if self.stderr:
            message += f"\n{self.stderr}"
        if self.partition_description or self.stdout or self.stderr:
            message += "\n\n"

        return message

    def cacheable(self) -> bool:
        """Is marked uncacheable to ensure that it always renders."""
        return False


@runtime_ignore_subscripts
class Partitions(FrozenDict[Any, "tuple[_PartitionElementT, ...]"]):
    """A mapping from <partition key> to <partition>.

    When implementing a linter, one of your rules will return this type, taking in a
    `PartitionRequest` specific to your linter.

    The return likely will fit into one of:
        - Returning an empty partition: E.g. if your tool is being skipped.
        - Returning one partition. The partition may contain all of the inputs
            (as will likely be the case for target linters) or a subset (which will likely be the
            case for targetless linters).
        - Returning >1 partition. This might be the case if you can't run
            the tool on all the inputs at once. E.g. having to run a Python tool on XYZ with Py3,
            and files ABC with Py2.

    The partition key can be of any type able to cross a rule-boundary, and will be provided to the
    rule which "runs" your tool.

    NOTE: The partition may be divided further into multiple sub-partitions.
    """

    @classmethod
    def single_partition(
        cls, elements: Iterable[_PartitionElementT], key: Any = None
    ) -> Partitions[_PartitionElementT]:
        """Helper constructor for implementations that have only one partition."""
        return Partitions([(key, tuple(elements))])


@union
class LintRequest:
    """Base class for plugin types wanting to be run as part of `lint`.

    Plugins should define a new type which subclasses either `LintTargetsRequest` (to lint targets)
    or `LintFilesRequest` (to lint arbitrary files), and set the appropriate class variables.
    E.g.
        class DryCleaningRequest(LintTargetsRequest):
            name = DryCleaningSubsystem.options_scope
            field_set_type = DryCleaningFieldSet

    Then, define 2 `@rule`s:
        1. A rule which takes an instance of your request type's `PartitionRequest` class property,
            and returns a `Partitions` instance.
            E.g.
                @rule
                async def partition(
                    request: DryCleaningRequest.PartitionRequest[DryCleaningFieldSet]
                    # or `request: DryCleaningRequest.PartitionRequest` if file linter
                    subsystem: DryCleaningSubsystem,
                ) -> Partitions[DryCleaningFieldSet]:
                    if subsystem.skip:
                        return Partitions()

                    # One possible implementation
                    return Partitions.single_partition(request.field_sets)

        2. A rule which takes an instance of your request type's `SubPartition` class property, and
            returns a `LintResult instance.
            E.g.
                @rule
                async def dry_clean(
                    request: DryCleaningRequest.SubPartition,
                ) -> LintResult:
                    ...

    Lastly, register the rules which tell Pants about your plugin.
    E.g.
        def rules():
            return [
                *collect_rules(),
                *DryCleaningRequest.registration_rules()
            ]

    NOTE: For more information about the `PartitionRequest` types, see
        `LintTargetsRequest.PartitionRequest`/`LintFilesRequest.PartitionRequest`.
    """

    tool_name: ClassVar[str]
    is_formatter: ClassVar[bool] = False

    @distinct_union_type_per_subclass(in_scope_types=[EnvironmentName])
    # NB: Not frozen so `fmt` can subclass
    @frozen_after_init
    @dataclass(unsafe_hash=True)
    @runtime_ignore_subscripts
    class SubPartition(Generic[_PartitionElementT]):
        elements: Tuple[_PartitionElementT, ...]
        key: Any

    @final
    @classmethod
    def registration_rules(cls) -> Iterable[UnionRule]:
        yield from cls._get_registration_rules()

    @classmethod
    def _get_registration_rules(cls) -> Iterable[UnionRule]:
        yield UnionRule(LintRequest, cls)
        yield UnionRule(LintRequest.SubPartition, cls.SubPartition)


class LintTargetsRequest(LintRequest):
    """The entry point for linters that operate on targets."""

    field_set_type: ClassVar[type[FieldSet]]

    @distinct_union_type_per_subclass(in_scope_types=[EnvironmentName])
    @dataclass(frozen=True)
    @runtime_ignore_subscripts
    class PartitionRequest(Generic[_FieldSetT]):
        """Returns a unique `PartitionRequest` type per calling type.

        This serves us 2 purposes:
            1. `LintTargetsRequest.PartitionRequest` is the unique type used as a union base for plugin registration.
            2. `<Plugin Defined Subclass>.PartitionRequest` is the unique type used as the union member.
        """

        field_sets: tuple[_FieldSetT, ...]

    @classmethod
    def _get_registration_rules(cls) -> Iterable[UnionRule]:
        yield from super()._get_registration_rules()
        yield UnionRule(LintTargetsRequest.PartitionRequest, cls.PartitionRequest)


class LintFilesRequest(LintRequest, EngineAwareParameter):
    """The entry point for linters that do not use targets."""

    @distinct_union_type_per_subclass(in_scope_types=[EnvironmentName])
    @dataclass(frozen=True)
    class PartitionRequest:
        """Returns a unique `PartitionRequest` type per calling type.

        This serves us 2 purposes:
            1. `LintFilesRequest.PartitionRequest` is the unique type used as a union base for plugin registration.
            2. `<Plugin Defined Subclass>.PartitionRequest` is the unique type used as the union member.
        """

        files: tuple[str, ...]

    @classmethod
    def _get_registration_rules(cls) -> Iterable[UnionRule]:
        yield from super()._get_registration_rules()
        yield UnionRule(LintFilesRequest.PartitionRequest, cls.PartitionRequest)


# If a user wants linter reports to show up in dist/ they must ensure that the reports
# are written under this directory. E.g.,
# ./pants --flake8-args="--output-file=reports/report.txt" lint <target>
REPORT_DIR = "reports"


class LintSubsystem(GoalSubsystem):
    name = "lint"
    help = "Run all linters and/or formatters in check mode."

    @classmethod
    def activated(cls, union_membership: UnionMembership) -> bool:
        return LintRequest in union_membership

    only = OnlyOption("linter", "flake8", "shellcheck")
    skip_formatters = BoolOption(
        default=False,
        help=softwrap(
            f"""
            If true, skip running all formatters in check-only mode.

            FYI: when running `{bin_name()} fmt lint ::`, there should be diminishing performance
            benefit to using this flag. Pants attempts to reuse the results from `fmt` when running
            `lint` where possible.
            """
        ),
    )
    batch_size = BatchSizeOption(uppercase="Linter", lowercase="linter")


class Lint(Goal):
    subsystem_cls = LintSubsystem


def _print_results(
    console: Console,
    results_by_tool: dict[str, list[LintResult]],
    formatter_failed: bool,
) -> None:
    if results_by_tool:
        console.print_stderr("")

    for tool_name in sorted(results_by_tool):
        results = results_by_tool[tool_name]
        if any(result.exit_code for result in results):
            sigil = console.sigil_failed()
            status = "failed"
        else:
            sigil = console.sigil_succeeded()
            status = "succeeded"
        console.print_stderr(f"{sigil} {tool_name} {status}.")

    if formatter_failed:
        console.print_stderr("")
        console.print_stderr(f"(One or more formatters failed. Run `{bin_name()} fmt` to fix.)")


def _get_error_code(results: Sequence[LintResult]) -> int:
    for result in reversed(results):
        if result.exit_code:
            return result.exit_code
    return 0


_CoreRequestType = TypeVar("_CoreRequestType", bound=LintRequest)
_TargetPartitioner = TypeVar("_TargetPartitioner", bound=LintTargetsRequest.PartitionRequest)
_FilePartitioner = TypeVar("_FilePartitioner", bound=LintFilesRequest.PartitionRequest)


@rule_helper
async def _get_partitions_by_request_type(
    core_request_types: Iterable[type[_CoreRequestType]],
    target_partitioners: Iterable[type[_TargetPartitioner]],
    file_partitioners: Iterable[type[_FilePartitioner]],
    subsystem: GoalSubsystem,
    specs: Specs,
    # NB: Because the rule parser code will collect `Get`s from caller's scope, these allows the
    # caller to customize the specific `Get`.
    make_targets_partition_request_get: Callable[[_TargetPartitioner], Get[Partitions]],
    make_files_partition_request_get: Callable[[_FilePartitioner], Get[Partitions]],
) -> dict[type[_CoreRequestType], list[Partitions]]:
    specified_names = determine_specified_tool_names(
        subsystem.name,
        subsystem.only,  # type: ignore[attr-defined]
        core_request_types,
    )

    filtered_core_request_types = [
        request_type
        for request_type in core_request_types
        if request_type.tool_name in specified_names
    ]
    if not filtered_core_request_types:
        return {}

    core_partition_request_types = {
        getattr(request_type, "PartitionRequest") for request_type in filtered_core_request_types
    }
    target_partitioners = [
        target_partitioner
        for target_partitioner in target_partitioners
        if target_partitioner in core_partition_request_types
    ]
    file_partitioners = [
        file_partitioner
        for file_partitioner in file_partitioners
        if file_partitioner in core_partition_request_types
    ]

    _get_targets = Get(
        FilteredTargets,
        Specs,
        specs if target_partitioners else Specs.empty(),
    )
    _get_specs_paths = Get(SpecsPaths, Specs, specs if file_partitioners else Specs.empty())

    targets, specs_paths = await MultiGet(_get_targets, _get_specs_paths)

    def partition_request_get(request_type: type[LintRequest]) -> Get[Partitions]:
        partition_request_type: type = getattr(request_type, "PartitionRequest")
        if partition_request_type in target_partitioners:
            partition_targets_type = cast(LintTargetsRequest, request_type)
            field_set_type = partition_targets_type.field_set_type
            field_sets = tuple(
                field_set_type.create(target)
                for target in targets
                if field_set_type.is_applicable(target)
            )
            return make_targets_partition_request_get(
                partition_targets_type.PartitionRequest(field_sets)  # type: ignore[arg-type]
            )
        else:
            assert partition_request_type in file_partitioners
            partition_files_type = cast(LintFilesRequest, request_type)
            return make_files_partition_request_get(
                partition_files_type.PartitionRequest(specs_paths.files)  # type: ignore[arg-type]
            )

    all_partitions = await MultiGet(
        partition_request_get(request_type) for request_type in filtered_core_request_types
    )
    partitions_by_request_type = defaultdict(list)
    for request_type, partition in zip(filtered_core_request_types, all_partitions):
        partitions_by_request_type[request_type].append(partition)

    return partitions_by_request_type


@goal_rule
async def lint(
    console: Console,
    workspace: Workspace,
    specs: Specs,
    lint_subsystem: LintSubsystem,
    union_membership: UnionMembership,
    dist_dir: DistDir,
) -> Lint:
    lint_request_types = union_membership.get(LintRequest)
    target_partitioners = union_membership.get(LintTargetsRequest.PartitionRequest)
    file_partitioners = union_membership.get(LintFilesRequest.PartitionRequest)

    partitions_by_request_type = await _get_partitions_by_request_type(
        [
            request_type
            for request_type in lint_request_types
            if not (request_type.is_formatter and lint_subsystem.skip_formatters)
        ],
        target_partitioners,
        file_partitioners,
        lint_subsystem,
        specs,
        lambda request_type: Get(Partitions, LintTargetsRequest.PartitionRequest, request_type),
        lambda request_type: Get(Partitions, LintFilesRequest.PartitionRequest, request_type),
    )

    if not partitions_by_request_type:
        return Lint(exit_code=0)

    def batch(
        iterable: Iterable[_T], key: Callable[[_T], str] = lambda x: str(x)
    ) -> Iterator[tuple[_T, ...]]:
        batches = partition_sequentially(
            iterable,
            key=key,
            size_target=lint_subsystem.batch_size,
            size_max=4 * lint_subsystem.batch_size,
        )
        for batch in batches:
            yield tuple(batch)

    lint_batches_by_request_type = {
        request_type: [
            (subpartition, key)
            for partitions in partitions_list
            for key, partition in partitions.items()
            for subpartition in batch(partition)
        ]
        for request_type, partitions_list in partitions_by_request_type.items()
    }

    formatter_snapshots = await MultiGet(
        Get(Snapshot, PathGlobs(elements))
        for request_type, batch in lint_batches_by_request_type.items()
        for elements, _ in batch
        if request_type.is_formatter
    )
    snapshots_iter = iter(formatter_snapshots)

    subpartitions = [
        request_type.SubPartition(
            elements, key, **{"snapshot": next(snapshots_iter)} if request_type.is_formatter else {}
        )
        for request_type, batch in lint_batches_by_request_type.items()
        for elements, key in batch
    ]

    all_batch_results = await MultiGet(
        Get(LintResult, LintRequest.SubPartition, request) for request in subpartitions
    )

    core_request_types_by_subpartition_type = {
        request_type.SubPartition: request_type for request_type in lint_request_types
    }

    formatter_failed = any(
        result.exit_code
        for subpartition, result in zip(subpartitions, all_batch_results)
        if core_request_types_by_subpartition_type[type(subpartition)].is_formatter
    )

    results_by_tool = defaultdict(list)
    for result in all_batch_results:
        results_by_tool[result.linter_name].append(result)

    write_reports(
        results_by_tool,
        workspace,
        dist_dir,
        goal_name=LintSubsystem.name,
    )

    _print_results(
        console,
        results_by_tool,
        formatter_failed,
    )
    return Lint(_get_error_code(all_batch_results))


def rules():
    return collect_rules()
