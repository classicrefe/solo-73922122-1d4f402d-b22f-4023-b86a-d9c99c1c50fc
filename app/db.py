"""SQLite persistence.

Schema
------
original_scores     original table, inserted once per upload and never
                    modified afterwards.  ``raw_score`` is the score as
                    entered on the paper form; ``batch_id`` is the
                    distribution batch the household belongs to.
corrected_scores    working table; starts as a copy of the original and is
                    edited one household at a time.  ``manual = 1`` marks
                    manually entered / edited scores (including no-op edits
                    with the same value) and supplementary households; these
                    keep their absolute forward scores through batch
                    direction flips.  ``manual = 0`` rows follow the current
                    direction of their batch (a flip converts them).
batches             one row per distribution batch with its current
                    direction: 0 = forward (score used as entered),
                    1 = reverse (10 minus the entered score).
orig_rounds / corr_rounds
                    per-round weighted summaries (sum of weight, sum of
                    weighted score), keyed by round and bucket.  A bucket is
                    either a real batch id or ``MANUAL_BATCH``, which pools
                    every manual correction.  Analysis reads only these
                    summary tables plus ``batch_summary`` / ``meta`` and
                    never touches per-household detail.
batch_summary       per-bucket count and unweighted score sum, again split
                    into real batches and the manual bucket; this is all the
                    information a direction flip needs.
meta                scalar key/value bookkeeping (global totals, upload
                    timestamp and schema version).

Direction flips touch *only* the summary tables (round tables,
``batch_summary`` and global ``meta`` totals), never the household detail
tables.  Everything changes in one transaction, so a failure leaves the
previous direction and the previous summaries in place.

Databases created by earlier versions (two-column CSV world, no batches) are
migrated in place on first open and keep working exactly as before.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .bootstrap import ROUNDS, household_weights

#: Pseudo-bucket collecting every manual / supplementary household.  The NUL
#: character is rejected in CSV batch ids, so it can never collide with a
#: real distribution batch.
MANUAL_BATCH = "\x00__manual__"
#: Two-column uploads (and blank third-column cells) land in this batch.
DEFAULT_BATCH = ""

DIRECTION_FORWARD = 0
DIRECTION_REVERSE = 1
REVERSE_BASE = 10
SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS original_scores (
    household_id TEXT PRIMARY KEY,
    raw_score    INTEGER NOT NULL CHECK (raw_score BETWEEN 0 AND 10),
    batch_id     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corrected_scores (
    household_id TEXT PRIMARY KEY,
    score        INTEGER NOT NULL CHECK (score BETWEEN 0 AND 10),
    manual       INTEGER NOT NULL DEFAULT 0 CHECK (manual IN (0, 1)),
    batch_id     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    batch_id  TEXT PRIMARY KEY,
    direction INTEGER NOT NULL DEFAULT 0 CHECK (direction IN (0, 1))
);
CREATE TABLE IF NOT EXISTS orig_rounds (
    round_index  INTEGER NOT NULL CHECK (round_index BETWEEN 0 AND 999),
    batch_id     TEXT NOT NULL,
    weight_total INTEGER NOT NULL,
    weighted_sum INTEGER NOT NULL,
    PRIMARY KEY (round_index, batch_id)
);
CREATE TABLE IF NOT EXISTS corr_rounds (
    round_index  INTEGER NOT NULL CHECK (round_index BETWEEN 0 AND 999),
    bucket       TEXT NOT NULL,
    weight_total INTEGER NOT NULL,
    weighted_sum INTEGER NOT NULL,
    PRIMARY KEY (round_index, bucket)
);
CREATE TABLE IF NOT EXISTS batch_summary (
    table_name TEXT NOT NULL CHECK (table_name IN ('orig', 'corr')),
    bucket     TEXT NOT NULL,
    count      INTEGER NOT NULL,
    score_sum  INTEGER NOT NULL,
    PRIMARY KEY (table_name, bucket)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

META_ORIG_SUM = "orig_score_sum"
META_ORIG_N = "orig_count"
META_CORR_SUM = "corr_score_sum"
META_CORR_N = "corr_count"
META_UPLOADED_AT = "uploaded_at"
META_SCHEMA_VERSION = "schema_version"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    """Migrate a v1 database in place; no-op for fresh / current databases."""
    conn.executescript(SCHEMA)
    version = conn.execute(
        "SELECT value FROM meta WHERE key=?", (META_SCHEMA_VERSION,)
    ).fetchone()
    if version is not None and version["value"] == SCHEMA_VERSION:
        conn.commit()
        return

    orig_cols = _table_columns(conn, "original_scores")
    corr_cols = _table_columns(conn, "corrected_scores")
    if not orig_cols or not corr_cols:
        # brand-new database: just stamp the version
        _set_meta(conn, META_SCHEMA_VERSION, SCHEMA_VERSION)
        conn.commit()
        return

    # --- v1 -> v2 migration, all in one transaction ---------------------
    with conn:
        old_orig_cols = orig_cols
        old_corr_cols = corr_cols
        if "batch_id" not in old_orig_cols:
            conn.execute(
                "ALTER TABLE original_scores RENAME COLUMN score TO raw_score"
            )
            conn.execute(
                "ALTER TABLE original_scores ADD COLUMN batch_id TEXT NOT NULL "
                f"DEFAULT {_sql_text(DEFAULT_BATCH)}"
            )
        if "batch_id" not in old_corr_cols:
            conn.execute(
                "ALTER TABLE corrected_scores ADD COLUMN batch_id TEXT NOT NULL "
                f"DEFAULT {_sql_text(DEFAULT_BATCH)}"
            )
        if "manual" not in old_corr_cols:
            conn.execute(
                "ALTER TABLE corrected_scores ADD COLUMN manual INTEGER NOT NULL "
                "DEFAULT 0 CHECK (manual IN (0, 1))"
            )
        # manual = corrected household whose score differs from the original,
        # or a supplementary household that has no original-table row at all
        conn.execute(
            "UPDATE corrected_scores SET manual = 1 WHERE household_id IN ("
            "SELECT c.household_id FROM corrected_scores c "
            "JOIN original_scores o ON o.household_id = c.household_id "
            "WHERE c.score <> o.raw_score)"
        )
        conn.execute(
            "UPDATE corrected_scores SET manual = 1 "
            "WHERE household_id NOT IN (SELECT household_id FROM original_scores)"
        )
        conn.execute(
            "INSERT INTO batches(batch_id, direction) VALUES(?, ?) "
            "ON CONFLICT(batch_id) DO NOTHING",
            (DEFAULT_BATCH, DIRECTION_FORWARD),
        )

        # rebuild the round summary tables with the (round, bucket) key
        conn.execute("DROP TABLE orig_rounds")
        conn.execute("DROP TABLE corr_rounds")
        conn.executescript(
            """
            CREATE TABLE orig_rounds (
                round_index  INTEGER NOT NULL CHECK (round_index BETWEEN 0 AND 999),
                batch_id     TEXT NOT NULL,
                weight_total INTEGER NOT NULL,
                weighted_sum INTEGER NOT NULL,
                PRIMARY KEY (round_index, batch_id)
            );
            CREATE TABLE corr_rounds (
                round_index  INTEGER NOT NULL CHECK (round_index BETWEEN 0 AND 999),
                bucket       TEXT NOT NULL,
                weight_total INTEGER NOT NULL,
                weighted_sum INTEGER NOT NULL,
                PRIMARY KEY (round_index, bucket)
            );
            """
        )
        # original: everything in the default forward batch
        orig_rows = conn.execute(
            "SELECT household_id, raw_score FROM original_scores"
        ).fetchall()
        conn.executemany(
            "INSERT INTO orig_rounds(round_index, batch_id, weight_total, weighted_sum) "
            "VALUES(?, ?, ?, ?)",
            _round_bucket_rows(
                [(r["household_id"], r["raw_score"]) for r in orig_rows],
                DEFAULT_BATCH,
            ),
        )
        # corrected: following households stay in the default batch, manual
        # ones move into the manual bucket
        corr_rows = conn.execute(
            "SELECT household_id, score, manual FROM corrected_scores"
        ).fetchall()
        following = [
            (r["household_id"], r["score"]) for r in corr_rows if not r["manual"]
        ]
        manual = [(r["household_id"], r["score"]) for r in corr_rows if r["manual"]]
        if following:
            conn.executemany(
                "INSERT INTO corr_rounds(round_index, bucket, weight_total, weighted_sum) "
                "VALUES(?, ?, ?, ?)",
                _round_bucket_rows(following, DEFAULT_BATCH),
            )
        if manual:
            conn.executemany(
                "INSERT INTO corr_rounds(round_index, bucket, weight_total, weighted_sum) "
                "VALUES(?, ?, ?, ?)",
                _round_bucket_rows(manual, MANUAL_BATCH),
            )

        _rebuild_batch_summary(conn)
        _set_meta(conn, META_SCHEMA_VERSION, SCHEMA_VERSION)


def init_db(conn: sqlite3.Connection) -> None:
    migrate(conn)


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _round_rows(household_id: str, score: int) -> list[tuple[int, int, int]]:
    weights = household_weights(household_id)
    return [(r, w, w * score) for r, w in enumerate(weights)]


def _round_bucket_rows(
    households: list[tuple[str, int]], bucket: str
) -> list[tuple[int, int, int, int]]:
    """Aggregate the per-round contributions of all households in a bucket."""
    totals = [(r, 0, 0) for r in range(ROUNDS)]
    for household_id, score in households:
        for r, w, ws in _round_rows(household_id, score):
            totals[r] = (r, totals[r][1] + w, totals[r][2] + ws)
    return [(r, bucket, w, ws) for r, w, ws in totals]


def _set_meta(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _meta_int(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return int(row["value"]) if row is not None else 0


def _upsert_summary(
    conn: sqlite3.Connection, table_name: str, bucket: str,
    d_count: int, d_sum: int,
) -> None:
    conn.execute(
        "INSERT INTO batch_summary(table_name, bucket, count, score_sum) "
        "VALUES(?, ?, ?, ?) "
        "ON CONFLICT(table_name, bucket) DO UPDATE SET "
        "count = count + excluded.count, score_sum = score_sum + excluded.score_sum",
        (table_name, bucket, d_count, d_sum),
    )


def _rebuild_batch_summary(conn: sqlite3.Connection) -> None:
    """Recompute ``batch_summary`` from the detail tables (migration/reset)."""
    conn.execute("DELETE FROM batch_summary")
    conn.execute(
        "INSERT INTO batch_summary(table_name, bucket, count, score_sum) "
        "SELECT 'orig', batch_id, COUNT(*), SUM(raw_score) "
        "FROM original_scores GROUP BY batch_id"
    )
    conn.execute(
        "INSERT INTO batch_summary(table_name, bucket, count, score_sum) "
        "SELECT 'corr', CASE WHEN manual = 1 THEN ? ELSE batch_id END, "
        "COUNT(*), SUM(score) FROM corrected_scores GROUP BY 2",
        (MANUAL_BATCH,),
    )


def converted_score(raw_score: int, direction: int) -> int:
    """Apply a batch direction to an entered score."""
    return REVERSE_BASE - raw_score if direction == DIRECTION_REVERSE else raw_score


def replace_original(conn: sqlite3.Connection,
                     rows: list[tuple[str, int, str]],
                     uploaded_at: str) -> None:
    """Atomically replace the whole dataset.

    The original table (raw scores + batch ids), the corrected copy, the
    batch directory, both round summaries, the per-bucket summary and the
    scalar meta values are rebuilt in one transaction.  All batches start in
    the forward direction; the corrected table starts as a non-manual copy
    following those batches.
    """
    batches = sorted({batch_id for _, _, batch_id in rows})
    orig_detail = [(hid, score, batch_id) for hid, score, batch_id in rows]
    corr_detail = [(hid, score, 0, batch_id) for hid, score, batch_id in rows]

    with conn:  # COMMIT on success, ROLLBACK on any exception
        conn.execute("DELETE FROM original_scores")
        conn.execute("DELETE FROM corrected_scores")
        conn.execute("DELETE FROM orig_rounds")
        conn.execute("DELETE FROM corr_rounds")
        conn.execute("DELETE FROM batches")
        conn.execute("DELETE FROM batch_summary")

        conn.executemany(
            "INSERT INTO original_scores(household_id, raw_score, batch_id) "
            "VALUES(?, ?, ?)",
            orig_detail,
        )
        conn.executemany(
            "INSERT INTO corrected_scores(household_id, score, manual, batch_id) "
            "VALUES(?, ?, ?, ?)",
            corr_detail,
        )
        conn.executemany(
            "INSERT INTO batches(batch_id, direction) VALUES(?, ?)",
            [(b, DIRECTION_FORWARD) for b in batches],
        )

        for batch_id in batches:
            members = [(hid, score) for hid, score, b in rows if b == batch_id]
            conn.executemany(
                "INSERT INTO orig_rounds(round_index, batch_id, weight_total, weighted_sum) "
                "VALUES(?, ?, ?, ?)",
                _round_bucket_rows(members, batch_id),
            )
            conn.executemany(
                "INSERT INTO corr_rounds(round_index, bucket, weight_total, weighted_sum) "
                "VALUES(?, ?, ?, ?)",
                _round_bucket_rows(members, batch_id),
            )

        _rebuild_batch_summary(conn)
        total_score = sum(score for _, score, _ in rows)
        _set_meta(conn, META_ORIG_SUM, total_score)
        _set_meta(conn, META_ORIG_N, len(rows))
        _set_meta(conn, META_CORR_SUM, total_score)
        _set_meta(conn, META_CORR_N, len(rows))
        _set_meta(conn, META_UPLOADED_AT, uploaded_at)
        _set_meta(conn, META_SCHEMA_VERSION, SCHEMA_VERSION)


def _adjust_corr_rounds(
    conn: sqlite3.Connection, bucket: str,
    household_id: str, old_score: int | None, new_score: int | None,
) -> None:
    """Recompute one household's contribution inside one corr bucket."""
    if old_score is not None:
        conn.executemany(
            "UPDATE corr_rounds SET weight_total = weight_total - ?, "
            "weighted_sum = weighted_sum - ? WHERE round_index = ? AND bucket = ?",
            [(w, ws, r, bucket) for r, w, ws in _round_rows(household_id, old_score)],
        )
    if new_score is not None:
        conn.executemany(
            "INSERT INTO corr_rounds(round_index, bucket, weight_total, weighted_sum) "
            "VALUES(?, ?, ?, ?) "
            "ON CONFLICT(round_index, bucket) DO UPDATE SET "
            "weight_total = weight_total + excluded.weight_total, "
            "weighted_sum = weighted_sum + excluded.weighted_sum",
            [
                (r, bucket, w, ws)
                for r, w, ws in _round_rows(household_id, new_score)
            ],
        )


def _effective_corr_score(conn: sqlite3.Connection, existing: sqlite3.Row) -> int:
    """Current effective score of a corrected-table row.

    Manual rows store absolute forward scores.  Following rows store the
    entered raw score, so the batch's current direction must be applied.
    """
    if existing["manual"]:
        return existing["score"]
    row = conn.execute(
        "SELECT COALESCE(direction, 0) AS d FROM batches WHERE batch_id = ?",
        (existing["batch_id"],),
    ).fetchone()
    direction = row["d"] if row is not None else DIRECTION_FORWARD
    return converted_score(existing["score"], direction)


def apply_correction(conn: sqlite3.Connection, household_id: str,
                     action: str, new_score: int | None) -> None:
    """Apply one add / score-change / removal, atomically with its summary.

    Manually entered scores (including same-value edits and supplementary
    households) are stored as absolute forward scores in the manual bucket
    and are kept across direction flips.  Only the edited household's
    per-round contribution is recomputed; every other household is untouched.
    """
    with conn:  # read and write happen in one transaction
        existing = conn.execute(
            "SELECT score, manual, batch_id FROM corrected_scores "
            "WHERE household_id = ?",
            (household_id,),
        ).fetchone()
        original = conn.execute(
            "SELECT raw_score, batch_id FROM original_scores "
            "WHERE household_id = ?",
            (household_id,),
        ).fetchone()

        if action == "remove":
            if existing is None:
                raise ValueError(f"住户 {household_id} 不在更正表中，无法移除")
            old_bucket = (
                MANUAL_BATCH if existing["manual"] else existing["batch_id"]
            )
            old_effective = _effective_corr_score(conn, existing)
            _adjust_corr_rounds(
                conn, old_bucket, household_id, old_effective, None
            )
            conn.execute(
                "DELETE FROM corrected_scores WHERE household_id = ?",
                (household_id,),
            )
            conn.execute(
                "UPDATE batch_summary SET count = count - 1, "
                "score_sum = score_sum - ? WHERE table_name='corr' AND bucket=?",
                (old_effective, old_bucket),
            )
            conn.execute(
                "UPDATE meta SET value = CAST(value AS INTEGER) - ? "
                f"WHERE key = '{META_CORR_SUM}'",
                (old_effective,),
            )
            conn.execute(
                f"UPDATE meta SET value = CAST(value AS INTEGER) - 1 "
                f"WHERE key = '{META_CORR_N}'"
            )
            return

        if action not in ("add", "update"):
            raise ValueError(f"未知操作: {action}")
        if new_score is None or not 0 <= new_score <= 10:
            raise ValueError("评分必须是 0～10 的整数（正向分值）")

        if existing is None:
            if action == "update":
                raise ValueError(
                    f"住户 {household_id} 不在更正表中，无法改分；如需新增请选择补录"
                )
            batch_id = original["batch_id"] if original else MANUAL_BATCH
            conn.execute(
                "INSERT INTO corrected_scores(household_id, score, manual, batch_id) "
                "VALUES(?, ?, 1, ?)",
                (household_id, new_score, batch_id),
            )
            _adjust_corr_rounds(
                conn, MANUAL_BATCH, household_id, None, new_score
            )
            _upsert_summary(conn, "corr", MANUAL_BATCH, 1, new_score)
            conn.execute(
                "UPDATE meta SET value = CAST(value AS INTEGER) + ? "
                f"WHERE key = '{META_CORR_SUM}'",
                (new_score,),
            )
            conn.execute(
                f"UPDATE meta SET value = CAST(value AS INTEGER) + 1 "
                f"WHERE key = '{META_CORR_N}'"
            )
            return

        if action == "add":
            raise ValueError(
                f"住户 {household_id} 已在更正表中（评分 {existing['score']}），"
                "如需修改请选择改分"
            )

        # update — even a same-value edit counts as a manual correction: the
        # household is detached from its batch and moved to the manual bucket
        old_bucket = MANUAL_BATCH if existing["manual"] else existing["batch_id"]
        old_effective = _effective_corr_score(conn, existing)
        _adjust_corr_rounds(
            conn, old_bucket, household_id, old_effective, None
        )
        _adjust_corr_rounds(
            conn, MANUAL_BATCH, household_id, None, new_score
        )
        conn.execute(
            "UPDATE corrected_scores SET score = ?, manual = 1, batch_id = ? "
            "WHERE household_id = ?",
            (new_score, existing["batch_id"], household_id),
        )
        conn.execute(
            "UPDATE batch_summary SET count = count - 1, "
            "score_sum = score_sum - ? WHERE table_name='corr' AND bucket=?",
            (old_effective, old_bucket),
        )
        _upsert_summary(conn, "corr", MANUAL_BATCH, 1, new_score)
        conn.execute(
            "UPDATE meta SET value = CAST(value AS INTEGER) + ? "
            f"WHERE key = '{META_CORR_SUM}'",
            (new_score - old_effective,),
        )
        # count unchanged on score update


def set_batch_direction(conn: sqlite3.Connection, batch_id: str,
                        direction: int) -> dict:
    """Flip one batch between forward and reverse scoring.

    Only the summary tables are read and written: the per-batch round rows of
    both tables, the per-bucket summary and the global ``meta`` totals are
    converted together in one transaction.  Manual / supplementary households
    (the manual bucket) are deliberately untouched.  Returns a small
    description dict used by the web layer.
    """
    if direction not in (DIRECTION_FORWARD, DIRECTION_REVERSE):
        raise ValueError("方向只能是正向（0）或反向（1）")
    with conn:
        batch = conn.execute(
            "SELECT direction FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise ValueError(f"发卷批号 {batch_id!r} 不存在，无法切换方向")
        old_direction = batch["direction"]
        if old_direction == direction:
            return {
                "batch_id": batch_id,
                "direction": direction,
                "changed": False,
                "households": 0,
            }

        rows = conn.execute(
            "SELECT table_name, bucket, count, score_sum FROM batch_summary "
            "WHERE bucket = ?",
            (batch_id,),
        ).fetchall()
        affected = {r["table_name"]: r for r in rows}
        n_households = 0
        for table_name, rounds_table in (("orig", "orig_rounds"),
                                         ("corr", "corr_rounds")):
            summary = affected.get(table_name)
            if summary is None or summary["count"] == 0:
                continue
            count = summary["count"]
            n_households = max(n_households, count)
            old_sum = summary["score_sum"]
            new_sum = REVERSE_BASE * count - old_sum
            # per-round: new weighted_sum = 10*weight_total - old weighted_sum
            conn.execute(
                f"UPDATE {rounds_table} SET weighted_sum = ? * weight_total - weighted_sum "
                "WHERE " + ("batch_id" if table_name == "orig" else "bucket") + " = ?",
                (REVERSE_BASE, batch_id),
            )
            conn.execute(
                "UPDATE batch_summary SET score_sum = ? "
                "WHERE table_name = ? AND bucket = ?",
                (new_sum, table_name, batch_id),
            )
            meta_key = META_ORIG_SUM if table_name == "orig" else META_CORR_SUM
            conn.execute(
                "UPDATE meta SET value = CAST(value AS INTEGER) + ? WHERE key = ?",
                (new_sum - old_sum, meta_key),
            )
        conn.execute(
            "UPDATE batches SET direction = ? WHERE batch_id = ?",
            (direction, batch_id),
        )
        return {
            "batch_id": batch_id,
            "direction": direction,
            "changed": True,
            "households": n_households,
        }


def list_batches(conn: sqlite3.Connection) -> list[dict]:
    """Batch directory with per-batch original/corrected counts (summary only)."""
    out = []
    batch_rows = conn.execute(
        "SELECT batch_id, direction FROM batches ORDER BY batch_id"
    ).fetchall()
    for b in batch_rows:
        orig = conn.execute(
            "SELECT count, score_sum FROM batch_summary "
            "WHERE table_name='orig' AND bucket=?",
            (b["batch_id"],),
        ).fetchone()
        corr = conn.execute(
            "SELECT count, score_sum FROM batch_summary "
            "WHERE table_name='corr' AND bucket=?",
            (b["batch_id"],),
        ).fetchone()
        out.append({
            "batch_id": b["batch_id"],
            "is_default": b["batch_id"] == DEFAULT_BATCH,
            "direction": b["direction"],
            "orig_count": orig["count"] if orig else 0,
            "orig_score_sum": orig["score_sum"] if orig else 0,
            "following_count": corr["count"] if corr else 0,
        })
    manual = conn.execute(
        "SELECT count, score_sum FROM batch_summary "
        "WHERE table_name='corr' AND bucket=?",
        (MANUAL_BATCH,),
    ).fetchone()
    if manual is not None:
        out.append({
            "batch_id": MANUAL_BATCH,
            "is_default": False,
            "direction": None,
            "orig_count": 0,
            "orig_score_sum": 0,
            "following_count": manual["count"],
            "manual_score_sum": manual["score_sum"],
        })
    return out


def reset_corrected(conn: sqlite3.Connection) -> None:
    """Reset the correction table back to the original under current directions.

    All manual edits and removals are discarded and supplementary households
    disappear; every original household follows its batch direction again.
    The corrected round summary, per-bucket summary and global totals are
    rebuilt in the same transaction.
    """
    with conn:
        directions = {
            r["batch_id"]: r["direction"]
            for r in conn.execute("SELECT batch_id, direction FROM batches")
        }
        orig = conn.execute(
            "SELECT household_id, raw_score, batch_id FROM original_scores"
        ).fetchall()
        conn.execute("DELETE FROM corrected_scores")
        conn.execute("DELETE FROM corr_rounds")
        conn.execute(
            "DELETE FROM batch_summary WHERE table_name='corr'"
        )
        conn.executemany(
            "INSERT INTO corrected_scores(household_id, score, manual, batch_id) "
            "VALUES(?, ?, 0, ?)",
            [
                (r["household_id"],
                 converted_score(r["raw_score"], directions.get(
                     r["batch_id"], DIRECTION_FORWARD)),
                 r["batch_id"])
                for r in orig
            ],
        )
        by_batch: dict[str, list[tuple[str, int]]] = {}
        total = 0
        for r in orig:
            score = converted_score(
                r["raw_score"], directions.get(r["batch_id"], DIRECTION_FORWARD)
            )
            total += score
            by_batch.setdefault(r["batch_id"], []).append(
                (r["household_id"], score)
            )
        for batch_id, members in by_batch.items():
            conn.executemany(
                "INSERT INTO corr_rounds(round_index, bucket, weight_total, weighted_sum) "
                "VALUES(?, ?, ?, ?)",
                _round_bucket_rows(members, batch_id),
            )
        conn.execute(
            "INSERT INTO batch_summary(table_name, bucket, count, score_sum) "
            "SELECT 'corr', batch_id, COUNT(*), SUM(score) "
            "FROM corrected_scores GROUP BY batch_id"
        )
        _set_meta(conn, META_CORR_SUM, total)
        _set_meta(conn, META_CORR_N, len(orig))


def get_household(conn: sqlite3.Connection, household_id: str) -> dict | None:
    """Single-household lookup; no other household's detail is read.

    Returns the entered (raw) score, the original-table score after applying
    the current batch direction, and the current corrected score.
    """
    o = conn.execute(
        "SELECT o.raw_score, o.batch_id, COALESCE(b.direction, 0) AS direction "
        "FROM original_scores o LEFT JOIN batches b ON b.batch_id = o.batch_id "
        "WHERE o.household_id = ?",
        (household_id,),
    ).fetchone()
    c = conn.execute(
        "SELECT score, manual, batch_id FROM corrected_scores "
        "WHERE household_id = ?",
        (household_id,),
    ).fetchone()
    if o is None and c is None:
        return None

    raw_score = o["raw_score"] if o else None
    original_converted = (
        converted_score(o["raw_score"], o["direction"]) if o else None
    )
    if c is None:
        corrected_converted = None
    elif c["manual"]:
        # manual scores are stored as absolute forward values
        corrected_converted = c["score"]
    else:
        # following households keep the entered raw score in the detail row;
        # their effective score always follows the current batch direction
        batch_direction = conn.execute(
            "SELECT COALESCE(direction, 0) AS d FROM batches WHERE batch_id = ?",
            (c["batch_id"],),
        ).fetchone()["d"]
        corrected_converted = converted_score(c["score"], batch_direction)
    return {
        "household_id": household_id,
        "raw_score": raw_score,
        "original_score": original_converted,
        "corrected_score": corrected_converted,
        "batch_id": o["batch_id"] if o else c["batch_id"],
        "direction": o["direction"] if o else None,
        "manual": bool(c["manual"]) if c else False,
        "removed": c is None and o is not None,
    }


def has_dataset(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM meta WHERE key = '{META_ORIG_N}'"
    ).fetchone()
    return row is not None
