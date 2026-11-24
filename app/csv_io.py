"""Uploaded-CSV parsing and validation.

Accepted format: UTF-8 text (BOM allowed), one household per row, either two
columns — ``住户号,评分`` — or three columns — ``住户号,评分,发卷批号``.

* household id: non-empty, unique across the whole file, no NUL character;
* score: an integer in 0..10;
* batch id (third column, optional): arbitrary non-empty text after stripping;
  a blank third column means the default (forward) batch.  NUL characters are
  rejected because they are reserved internally;
* a header row is auto-detected on the first line (when the second column is
  not parseable as an integer) and skipped regardless of its width;
* every data row must have the same number of columns (all two-column or all
  three-column); blank lines are ignored;
* any invalid record rejects the whole upload, so nothing is partially
  written to the database.

Two-column files keep their historical behaviour: every row is assigned to
the default batch, which is scored forward unless the analyst flips it on the
web page.
"""

from __future__ import annotations

import csv
import io

from .db import DEFAULT_BATCH


class CSVValidationError(ValueError):
    pass


def _clean_id(value: str, line_no: int, field: str) -> str:
    cleaned = value.strip()
    if cleaned == "":
        raise CSVValidationError(f"第 {line_no} 行{field}为空")
    if "\x00" in cleaned:
        raise CSVValidationError(f"第 {line_no} 行{field}含非法空字符（NUL）")
    return cleaned


def parse_scores(raw_bytes: bytes) -> list[tuple[str, int, str]]:
    """Parse an upload into ``(household_id, raw_score, batch_id)`` tuples.

    The score is always the score as entered on the paper form (the "raw"
    score); any 10-minus-score conversion for reverse batches happens in the
    storage layer.
    """
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CSVValidationError(f"文件不是有效的 UTF-8 文本：{exc}") from exc

    reader = csv.reader(io.StringIO(text))
    rows: list[tuple[str, int, str]] = []
    seen: set[str] = set()
    expected_width: int | None = None

    for line_no, raw in enumerate(reader, start=1):
        if not raw or all(cell.strip() == "" for cell in raw):
            continue  # tolerate blank lines
        if len(raw) not in (2, 3):
            raise CSVValidationError(
                f"第 {line_no} 行有 {len(raw)} 列，应为 2 列（住户号,评分）"
                "或 3 列（住户号,评分,发卷批号）"
            )

        household_id = _clean_id(raw[0], line_no, "住户号")
        score_text = raw[1].strip()
        try:
            score = int(score_text)
        except ValueError:
            if line_no == 1:
                # treat the first non-integer row as a header and skip it
                continue
            raise CSVValidationError(
                f"第 {line_no} 行评分 {score_text!r} 不是整数"
            ) from None

        # data row: its width pins the width expected of every other data row
        if expected_width is None:
            expected_width = len(raw)
        elif len(raw) != expected_width:
            raise CSVValidationError(
                f"第 {line_no} 行有 {len(raw)} 列，与首行数据的 "
                f"{expected_width} 列不一致（全表须同为两列或三列）"
            )

        if not 0 <= score <= 10:
            raise CSVValidationError(
                f"第 {line_no} 行评分 {score} 超出 0～10 的范围"
            )
        if household_id in seen:
            raise CSVValidationError(f"住户号 {household_id} 重复出现")
        seen.add(household_id)

        if expected_width == 3:
            batch_id = raw[2].strip()
            if batch_id == "":
                batch_id = DEFAULT_BATCH
            elif "\x00" in batch_id:
                raise CSVValidationError(
                    f"第 {line_no} 行发卷批号含非法空字符（NUL）"
                )
        else:
            batch_id = DEFAULT_BATCH
        rows.append((household_id, score, batch_id))

    if not rows:
        raise CSVValidationError("CSV 中没有有效的住户评分记录")
    return rows
