"""All-or-nothing tests for retained normalization results and output bundles."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import cast

import pytest

from kegg_mcp.domain import KoAnalysisView, build_ko_analysis_view
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.importers import (
    AnalysisViewImportLimits,
    SourceProvenanceInput,
    import_plain_ko,
    stream_deepkoala_analysis_view,
)
from kegg_mcp.services import annotation_analysis
from kegg_mcp.services.annotation_analysis import analyze_annotation_targets
from kegg_mcp.services.models import AnnotationInputFormat, NormalizeAnnotationsRequest
from kegg_mcp.services.normalization import normalize_annotations
from kegg_mcp.services.reference_budget import KeggPrimitiveClient
from kegg_mcp.services.reference_loading import PathwaySpec
from kegg_mcp.services.result_store import (
    ResultArtifactInput,
    ResultStoreError,
    SQLiteResultStore,
    create_retained_result,
)


def _request() -> NormalizeAnnotationsRequest:
    return NormalizeAnnotationsRequest(text="K00001\n")


def _analysis_view() -> KoAnalysisView:
    request = _request()
    assert request.text is not None
    dataset = import_plain_ko(
        request.text,
        limits=request.import_limits,
        analysis_unit=request.analysis_unit,
    )
    return build_ko_analysis_view(dataset, input_bytes=len(request.text.encode()))


def _analyze_with_pathway(
    store: SQLiteResultStore,
    output: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def load_references(*args: object, **kwargs: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(
        annotation_analysis,
        "load_pathway_references",
        load_references,
    )
    analyze_annotation_targets(
        _request(),
        module_ids=(),
        pathways=(PathwaySpec(pathway_id="map00010"),),
        client=cast(KeggPrimitiveClient, object()),
        result_store=store,
        scope_id="scope",
        analysis_view=_analysis_view(),
        output_directory=output,
    )


def test_result_create_failure_occurs_before_any_bundle_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    output = tmp_path / "bundle"

    def fail_create(*args: object, **kwargs: object) -> None:
        raise ResultStoreError("create")

    monkeypatch.setattr(store, "create", fail_create)

    with pytest.raises(ResultStoreError):
        normalize_annotations(
            _request(),
            result_store=store,
            scope_id="scope",
            output_directory=output,
        )

    assert not output.exists()


def test_retained_result_context_compensates_base_exception(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")

    with (
        pytest.raises(KeyboardInterrupt),
        create_retained_result(
            store,
            "scope",
            (ResultArtifactInput(section="detail", mime_type="application/json", content=b"{}"),),
        ),
    ):
        raise KeyboardInterrupt

    assert store.list_results("scope").total_items == 0


def test_bundle_failure_compensates_the_retained_result(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    output = tmp_path / "bundle"
    output.mkdir()
    (output / "bundle_manifest.json").write_text("occupied", encoding="utf-8")

    with pytest.raises(KeggMcpError) as caught:
        normalize_annotations(
            _request(),
            result_store=store,
            scope_id="scope",
            output_directory=output,
        )

    assert caught.value.detail.code is ErrorCode.OUTPUT_ALREADY_EXISTS
    assert store.list_results("scope").total_items == 0
    assert tuple(output.iterdir()) == (output / "bundle_manifest.json",)


def test_failed_result_compensation_is_escalated_as_internal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    output = tmp_path / "bundle"
    output.mkdir()
    (output / "bundle_manifest.json").write_text("occupied", encoding="utf-8")

    def fail_delete(*args: object, **kwargs: object) -> None:
        raise ResultStoreError("delete")

    monkeypatch.setattr(store, "delete", fail_delete)

    with pytest.raises(RuntimeError, match="compensation failed"):
        normalize_annotations(
            _request(),
            result_store=store,
            scope_id="scope",
            output_directory=output,
        )

    assert store.list_results("scope").total_items == 1


def test_analysis_result_create_failure_occurs_before_bundle_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    output = tmp_path / "bundle"
    bundle_called = False

    def fail_create(*args: object, **kwargs: object) -> None:
        raise ResultStoreError("create")

    def record_bundle(*args: object, **kwargs: object) -> None:
        nonlocal bundle_called
        bundle_called = True

    monkeypatch.setattr(store, "create", fail_create)
    monkeypatch.setattr(annotation_analysis, "write_analysis_bundle", record_bundle)

    with pytest.raises(ResultStoreError):
        _analyze_with_pathway(store, output, monkeypatch)

    assert not bundle_called
    assert not output.exists()


def test_analysis_rejects_streamed_view_from_a_different_importer_version(
    tmp_path: Path,
) -> None:
    payload = b"name,predict_label,probability,threshold,annotate\np1,K00001,0.9,0.5,*\n"
    input_path = str(tmp_path / "deepkoala.csv")
    source = SourceProvenanceInput(source_name="deepkoala", input_path=input_path)
    view = stream_deepkoala_analysis_view(
        BytesIO(payload),
        input_bytes=len(payload),
        source=source,
    )
    forged_source = view.sources[0].model_copy(update={"importer_version": "stale"})
    forged_view = view.model_copy(update={"sources": (forged_source,)})
    request = NormalizeAnnotationsRequest(
        file_path=input_path,
        input_format=AnnotationInputFormat.DEEPKOALA_DETAILED,
        source=source,
    )

    with pytest.raises(ValueError, match="analysis_view source"):
        analyze_annotation_targets(
            request,
            module_ids=(),
            pathways=(),
            client=cast(KeggPrimitiveClient, object()),
            result_store=SQLiteResultStore(tmp_path / "results.sqlite3"),
            scope_id="scope",
            analysis_view=forged_view,
            stream_import_limits=AnalysisViewImportLimits(),
        )


def test_analysis_bundle_failure_compensates_the_retained_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    output = tmp_path / "bundle"
    output.mkdir()
    manifest = output / "bundle_manifest.json"
    manifest.write_text("occupied", encoding="utf-8")

    with pytest.raises(KeggMcpError) as caught:
        _analyze_with_pathway(store, output, monkeypatch)

    assert caught.value.detail.code is ErrorCode.OUTPUT_ALREADY_EXISTS
    assert store.list_results("scope").total_items == 0
    assert tuple(output.iterdir()) == (manifest,)
