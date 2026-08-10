"""Streaming construction of a compact DeepKOALA accepted-KO analysis view."""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterator

from typing_extensions import Buffer

from kegg_mcp.domain.analysis_view import KoAnalysisView
from kegg_mcp.domain.annotations import (
    AnalysisUnit,
    DiagnosticCode,
    EvidenceField,
    ImportDiagnostic,
    NormalizedStatus,
    StatusCount,
)
from kegg_mcp.domain.decisions import DEEPKOALA_DETAILED
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.importers._common import (
    build_row_evidence,
    build_source,
    check_table_columns,
    configured_csv_field_limit,
    read_table_header,
    validate_auxiliary_evidence,
)
from kegg_mcp.importers.contracts import AnalysisViewImportLimits, SourceProvenanceInput
from kegg_mcp.importers.deepkoala import (
    parse_deepkoala_row,
    validate_deepkoala_header,
)


class _StreamInputLimit(Exception):
    def __init__(self, actual_bytes: int) -> None:
        super().__init__("streaming intake exceeds its hard byte limit")
        self.actual_bytes = actual_bytes


class _CountingBoundedReader(io.RawIOBase):
    """Bound any binary stream independently of MCP path intake and count consumed bytes."""

    def __init__(self, source: io.BufferedIOBase, max_bytes: int) -> None:
        super().__init__()
        self._source = source
        self._max_bytes = max_bytes
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer, /) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        view = memoryview(buffer)
        remaining_with_sentinel = self._max_bytes + 1 - self.bytes_read
        if remaining_with_sentinel <= 0:
            raise _StreamInputLimit(self.bytes_read)
        chunk = self._source.read(min(len(view), 65_536, remaining_with_sentinel))
        if not chunk:
            return 0
        self.bytes_read += len(chunk)
        if self.bytes_read > self._max_bytes:
            raise _StreamInputLimit(self.bytes_read)
        view[: len(chunk)] = chunk
        return len(chunk)


class _DiagnosticAccumulator:
    """Count every diagnostic while retaining only the configured leading preview."""

    def __init__(self, preview_limit: int) -> None:
        self._preview_limit = preview_limit
        self.count = 0
        self.preview: list[ImportDiagnostic] = []

    def extend(self, diagnostics: list[ImportDiagnostic]) -> None:
        self.count += len(diagnostics)
        remaining = self._preview_limit - len(self.preview)
        if remaining > 0:
            self.preview.extend(diagnostics[:remaining])

    def add(self, diagnostic: ImportDiagnostic) -> None:
        self.count += 1
        if len(self.preview) < self._preview_limit:
            self.preview.append(diagnostic)


def _bounded_csv_lines(
    stream: io.TextIOBase,
    limits: AnalysisViewImportLimits,
) -> Iterator[str]:
    """Yield physical CSV lines only while the current logical record remains bounded."""
    max_record_characters = limits.max_columns * (2 * limits.max_field_length + 3) + 2
    logical_characters = 0
    column_count = 1
    at_field_start = True
    in_quotes = False

    while True:
        remaining = max_record_characters - logical_characters
        line = stream.readline(remaining + 1)
        if not line:
            return
        if len(line) > remaining:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "A DeepKOALA CSV record exceeds the streaming intake record-size limit.",
                suggested_action="Split or repair the oversized logical CSV record.",
                safe_details=(
                    SafeDetail(
                        name="max_record_characters",
                        value=str(max_record_characters),
                    ),
                ),
            )
        logical_characters += len(line)

        if line.endswith("\r\n"):
            content = line[:-2]
            has_line_ending = True
        elif line.endswith(("\r", "\n")):
            content = line[:-1]
            has_line_ending = True
        else:
            content = line
            has_line_ending = False

        index = 0
        while index < len(content):
            character = content[index]
            if in_quotes:
                if character == '"':
                    if index + 1 < len(content) and content[index + 1] == '"':
                        index += 2
                        continue
                    in_quotes = False
                index += 1
                continue
            if at_field_start and character == '"':
                in_quotes = True
                at_field_start = False
            elif character == ",":
                column_count += 1
                if column_count > limits.max_columns:
                    fail(
                        ErrorCode.INPUT_LIMIT_EXCEEDED,
                        "A DeepKOALA CSV record exceeds the streaming intake column limit.",
                        suggested_action="Reduce the number of source columns and retry.",
                        safe_details=(
                            SafeDetail(name="max_columns", value=str(limits.max_columns)),
                        ),
                    )
                at_field_start = True
            else:
                at_field_start = False
            index += 1

        yield line
        if has_line_ending and not in_quotes:
            logical_characters = 0
            column_count = 1
            at_field_start = True


def stream_deepkoala_analysis_view(
    stream: io.BufferedIOBase,
    *,
    input_bytes: int,
    limits: AnalysisViewImportLimits | None = None,
    analysis_unit: AnalysisUnit = AnalysisUnit.UNKNOWN,
    taxon_id: int | None = None,
    kegg_organism_code: str | None = None,
    metadata: tuple[EvidenceField, ...] = (),
    source: SourceProvenanceInput | None = None,
) -> KoAnalysisView:
    """Stream detailed CSV into sorted unique accepted KOs and aggregate accounting only."""
    stream_limits = limits or AnalysisViewImportLimits()
    if input_bytes < 0 or input_bytes > stream_limits.max_bytes:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The annotation file exceeds the streamed analysis-view input size limit.",
            suggested_action="Provide a smaller DeepKOALA detailed-output file.",
            safe_details=(
                SafeDetail(name="max_bytes", value=str(stream_limits.max_bytes)),
                SafeDetail(name="actual_bytes", value=str(max(input_bytes, 0))),
            ),
        )
    validate_auxiliary_evidence(metadata, source, stream_limits)
    if source is not None and source.source_name != "deepkoala":
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "DeepKOALA detailed input requires source_name='deepkoala'.",
            suggested_action="Correct the source provenance before streaming the table.",
            safe_details=(SafeDetail(name="source_name", value=source.source_name),),
        )
    provenance = build_source(
        source,
        default_source_name="deepkoala",
        importer_name="deepkoala_analysis_view",
    )
    counts = {status: 0 for status in NormalizedStatus}
    accepted_ko_ids: set[str] = set()
    diagnostics = _DiagnosticAccumulator(stream_limits.max_diagnostic_preview)
    input_rows = 0
    expanded_assignments = 0
    skipped_rows = 0

    bounded_raw_stream = _CountingBoundedReader(stream, stream_limits.max_bytes)
    bounded_binary_stream = io.BufferedReader(bounded_raw_stream, buffer_size=65_536)
    text_stream = io.TextIOWrapper(
        bounded_binary_stream,
        encoding="utf-8-sig",
        errors="strict",
        newline="",
    )
    try:
        with configured_csv_field_limit(stream_limits):
            reader = csv.reader(
                _bounded_csv_lines(text_stream, stream_limits),
                delimiter=",",
                strict=True,
            )
            header = read_table_header(reader, stream_limits)
            has_domain_coordinates = validate_deepkoala_header(header)
            for cells in reader:
                if not cells:
                    continue
                input_rows += 1
                if input_rows > stream_limits.max_rows:
                    fail(
                        ErrorCode.INPUT_LIMIT_EXCEEDED,
                        "The annotation table exceeds the streaming intake row limit.",
                        suggested_action="Split the input into independently analyzed datasets.",
                        safe_details=(
                            SafeDetail(name="max_rows", value=str(stream_limits.max_rows)),
                        ),
                    )
                check_table_columns(cells, stream_limits)
                evidence = build_row_evidence(header, cells, reader.line_num)
                if len(cells) != len(header):
                    skipped_rows += 1
                    diagnostics.add(
                        ImportDiagnostic(
                            code=DiagnosticCode.ROW_SKIPPED,
                            message="The row has a different number of fields than the header.",
                            row_number=reader.line_num,
                            field=None,
                            safe_details=(
                                EvidenceField(name="expected_columns", value=len(header)),
                                EvidenceField(name="actual_columns", value=len(cells)),
                            ),
                        )
                    )
                    continue

                row_diagnostics: list[ImportDiagnostic] = []
                parsed = parse_deepkoala_row(
                    evidence,
                    has_domain_coordinates=has_domain_coordinates,
                    diagnostics=row_diagnostics,
                    remaining_assignment_capacity=(
                        stream_limits.max_expanded_assignments - expanded_assignments
                    ),
                    max_assignment_count=stream_limits.max_expanded_assignments,
                    assignment_limit_name="max_expanded_assignments",
                )
                diagnostics.extend(row_diagnostics)
                if parsed is None:
                    skipped_rows += 1
                    continue
                expanded_assignments += len(parsed.assignments)
                for assignment in parsed.assignments:
                    counts[assignment.outcome.status] += 1
                    if (
                        assignment.outcome.status is NormalizedStatus.ACCEPTED
                        and assignment.ko_id is not None
                    ):
                        if (
                            assignment.ko_id not in accepted_ko_ids
                            and len(accepted_ko_ids) >= stream_limits.max_unique_ko_ids
                        ):
                            fail(
                                ErrorCode.INPUT_LIMIT_EXCEEDED,
                                "Unique accepted K numbers exceed the analysis-view limit.",
                                suggested_action=(
                                    "Verify that the input uses canonical KEGG K numbers."
                                ),
                                safe_details=(
                                    SafeDetail(
                                        name="max_unique_ko_ids",
                                        value=str(stream_limits.max_unique_ko_ids),
                                    ),
                                ),
                            )
                        accepted_ko_ids.add(assignment.ko_id)
    except _StreamInputLimit as error:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The annotation file exceeds the streamed analysis-view input size limit.",
            suggested_action="Provide a smaller DeepKOALA detailed-output file.",
            safe_details=(
                SafeDetail(name="max_bytes", value=str(stream_limits.max_bytes)),
                SafeDetail(name="actual_bytes", value=str(error.actual_bytes)),
            ),
        )
    except UnicodeDecodeError:
        fail(
            ErrorCode.UNSUPPORTED_INPUT_FORMAT,
            "The annotation file is not valid UTF-8 text.",
            suggested_action="Convert the file to UTF-8 and retry.",
        )
    except csv.Error:
        fail(
            ErrorCode.INVALID_ANNOTATION_TABLE,
            "The annotation table is not well-formed delimited text.",
            suggested_action="Repair quoting or delimiters and try again.",
        )
    finally:
        text_stream.detach()
        bounded_binary_stream.close()

    if bounded_raw_stream.bytes_read != input_bytes:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The supplied annotation byte count does not match the streamed input.",
            suggested_action="Use the pinned annotation-file intake and its exact byte size.",
            safe_details=(
                SafeDetail(name="declared_bytes", value=str(input_bytes)),
                SafeDetail(name="observed_bytes", value=str(bounded_raw_stream.bytes_read)),
            ),
        )

    return KoAnalysisView(
        dataset_id=f"analysis-{uuid.uuid4().hex}",
        accepted_ko_ids=tuple(sorted(accepted_ko_ids)),
        input_bytes=input_bytes,
        input_rows=input_rows,
        assignment_count=expanded_assignments,
        skipped_rows=skipped_rows,
        source_columns=header,
        status_counts=tuple(
            StatusCount(status=status, count=counts[status]) for status in NormalizedStatus
        ),
        diagnostic_count=diagnostics.count,
        diagnostic_preview=tuple(diagnostics.preview),
        diagnostics_truncated=diagnostics.count > len(diagnostics.preview),
        decision_policy=DEEPKOALA_DETAILED.reference,
        sources=(provenance,),
        analysis_unit=analysis_unit,
        taxon_id=taxon_id,
        kegg_organism_code=kegg_organism_code,
        metadata=metadata,
    )


__all__ = ["stream_deepkoala_analysis_view"]
