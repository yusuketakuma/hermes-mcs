"""Synthetic geometry evaluation: upstream YomiToku 0.16.1 cell emission vs MCS rejection.

Loads the evaluation-only extracted pure functions (CC BY-NC-SA 4.0,
noncommercial technical evaluation) and checks that the documented
partial-containment mechanism produces a genuinely overlapping grid
which `attachment_ocr._document_text` rejects as output_schema_table_overlap.
"""
import importlib.util
from pathlib import Path

import pytest

import attachment_ocr as ocr

_UPSTREAM_PATH = (Path(__file__).resolve().parents[2]
                  / "evaluation/yomitoku_table_v0161/upstream_cells.py")
_spec = importlib.util.spec_from_file_location(
    "yomitoku_table_v0161_upstream_cells", _UPSTREAM_PATH)
upstream = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(upstream)


def _grid_2x2():
    """Two rows and two columns of 100x100 cells over a 200x200 table."""
    row_boxes = [[0, 0, 200, 100], [0, 100, 200, 200]]
    col_boxes = [[0, 0, 100, 200], [100, 0, 200, 200]]
    return upstream.extract_cells(row_boxes, col_boxes)


def test_partial_span_containment_leaves_overlapping_survivor_cell():
    # span covers most of the table but only ~72% of cell (1,1).
    cells = upstream.filter_contained_cells_within_spancell(_grid_2x2(), [[15, 15, 200, 200]])
    by_pos = {(c["row"], c["col"], c["row_span"], c["col_span"]) for c in cells}
    # Cell (1,1) survives at 0.7225 containment; the other three merge into a 2x2 span cell.
    assert (1, 1, 1, 1) in by_pos
    assert (1, 1, 2, 2) in by_pos
    assert len(cells) == 2


def test_mcs_document_text_rejects_the_overlapping_grid_as_table_overlap():
    cells = upstream.filter_contained_cells_within_spancell(_grid_2x2(), [[15, 15, 200, 200]])
    for cell in cells:
        cell["contents"] = "合成"
    with pytest.raises(ValueError, match="^output_schema_table_overlap$"):
        ocr._document_text({"paragraphs": [], "tables": [{"cells": cells}]})


def test_full_containment_merges_to_single_span_cell_and_text_renders():
    cells = upstream.filter_contained_cells_within_spancell(_grid_2x2(), [[0, 0, 200, 200]])
    assert [(c["row"], c["col"], c["row_span"], c["col_span"]) for c in cells] == [(1, 1, 2, 2)]
    cells[0]["contents"] = "合成見出し"
    assert ocr._document_text({"paragraphs": [], "tables": [{"cells": cells}]}) \
        == "\n\t合成見出し\t\n\t\t"


def test_grid_without_spans_renders_plain_table_and_blank_cells():
    cells = _grid_2x2()
    assert {(c["row"], c["col"]) for c in cells} == {(1, 1), (1, 2), (2, 1), (2, 2)}
    cells[0]["contents"], cells[3]["contents"] = "項目", "5 mg"
    assert ocr._document_text({"paragraphs": [], "tables": [{"cells": cells}]}) \
        == "\n\t項目\t\n\t\t5 mg"


@pytest.mark.parametrize("ratio_box,contained", [
    ([0, 0, 80, 100], False),   # 80x100/10000 = exactly 0.8 -> strict > rejects
    ([0, 0, 81, 100], True),    # 81x100/10000 = 0.81 -> absorbed
])
def test_containment_threshold_boundary_is_strict(ratio_box, contained):
    cell_box = [0, 0, 100, 100]
    assert upstream.is_contained(ratio_box, cell_box) is contained
