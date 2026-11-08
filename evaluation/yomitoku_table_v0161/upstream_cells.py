"""Pure table-cell geometry functions extracted verbatim from upstream YomiToku.

Source: https://github.com/kotaro-kinoshita/yomitoku
Commit: 0279cf256e2588430d085e2dc958e8740be82921 (tag v0.16.1)
YomiToku (c) 2024 by Kotaro Kinoshita is licensed under CC BY-NC-SA 4.0.
https://creativecommons.org/licenses/by-nc-sa/4.0/

The function bodies below are UNMODIFIED. They were extracted (imports
removed, six functions aggregated into this one file) from:

- src/yomitoku/table_structure_recognizer.py
    extract_cells                            (line 27)
    filter_contained_cells_within_spancell   (line 49)
- src/yomitoku/utils/misc.py
    filter_by_flag                           (line 37)
    calc_overlap_ratio                       (line 56)
    is_contained                             (line 74)
    calc_intersection                        (line 96)

File sha256 (first 16 hex, tag 0279cf25):
    table_structure_recognizer.py  52361dcd8fbd7480
    utils/misc.py                  2f9c59a39e3662cd

Evaluation-only module for noncommercial technical evaluation. Never
imported by product code paths.
"""


def calc_intersection(rect_a, rect_b):
    ax1, ay1, ax2, ay2 = map(int, rect_a)
    bx1, by1, bx2, by2 = map(int, rect_b)

    # 交差領域の左上と右下の座標
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    overlap_width = max(0, ix2 - ix1)
    overlap_height = max(0, iy2 - iy1)

    if overlap_width == 0 or overlap_height == 0:
        return None

    return [ix1, iy1, ix2, iy2]


def calc_overlap_ratio(rect_a, rect_b):
    intersection = calc_intersection(rect_a, rect_b)
    if intersection is None:
        return 0, None

    ix1, iy1, ix2, iy2 = intersection

    overlap_width = ix2 - ix1
    overlap_height = iy2 - iy1
    bx1, by1, bx2, by2 = rect_b

    b_area = (bx2 - bx1) * (by2 - by1)
    overlap_area = overlap_width * overlap_height

    overlap_ratio = overlap_area / b_area
    return overlap_ratio, intersection


def is_contained(rect_a, rect_b, threshold=0.8):
    """二つの矩形A, Bが与えられたとき、矩形Bが矩形Aに含まれるかどうかを判定する。
    ずれを許容するため、重複率求め、thresholdを超える場合にTrueを返す。


    Args:
        rect_a (np.array): x1, y1, x2, y2
        rect_b (np.array): x1, y1, x2, y2
        threshold (float, optional): 判定の閾値. Defaults to 0.9.

    Returns:
        bool: 矩形Bが矩形Aに含まれる場合True
    """

    overlap_ratio, _ = calc_overlap_ratio(rect_a, rect_b)

    if overlap_ratio > threshold:
        return True

    return False


def filter_by_flag(elements, flags):
    assert len(elements) == len(flags)
    return [element for element, flag in zip(elements, flags) if flag]


def extract_cells(row_boxes, col_boxes):
    cells = []
    for i, row_box in enumerate(row_boxes):
        for j, col_box in enumerate(col_boxes):
            intersection = calc_intersection(row_box, col_box)
            if intersection is None:
                continue

            cells.append(
                {
                    "col": j + 1,
                    "row": i + 1,
                    "col_span": 1,
                    "row_span": 1,
                    "box": intersection,
                    "contents": None,
                }
            )

    return cells


def filter_contained_cells_within_spancell(cells, span_boxes):
    check_list = [True] * len(cells)
    child_boxes = [[] for _ in range(len(span_boxes))]
    for i, span_box in enumerate(span_boxes):
        for j, sub_cell in enumerate(cells):
            if is_contained(span_box, sub_cell["box"]):
                check_list[j] = False
                child_boxes[i].append(sub_cell)

    cells = filter_by_flag(cells, check_list)

    for i, span_box in enumerate(span_boxes):
        child_box = child_boxes[i]

        if len(child_box) == 0:
            continue

        row = min([box["row"] for box in child_box])
        col = min([box["col"] for box in child_box])
        row_span = max([box["row"] for box in child_box]) - row + 1
        col_span = max([box["col"] for box in child_box]) - col + 1

        span_box = list(map(int, span_box))

        cells.append(
            {
                "col": col,
                "row": row,
                "col_span": col_span,
                "row_span": row_span,
                "box": span_box,
                "contents": None,
            }
        )

    cells = sorted(cells, key=lambda x: (x["row"], x["col"]))
    return cells
