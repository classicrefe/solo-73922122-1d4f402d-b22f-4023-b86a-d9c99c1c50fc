"""End-to-end tests: CSV upload, batch direction, corrections, summaries."""

import io
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.bootstrap import ROUNDS, household_weights  # noqa: E402
from app.csv_io import CSVValidationError, parse_scores  # noqa: E402
from app.db import (  # noqa: E402
    DEFAULT_BATCH,
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    MANUAL_BATCH,
    META_CORR_N,
    META_CORR_SUM,
    META_ORIG_N,
    META_ORIG_SUM,
    apply_correction,
    connect,
    get_household,
    init_db,
    list_batches,
    replace_original,
    reset_corrected,
    set_batch_direction,
)
from app.stats import analyze  # noqa: E402


def csv_bytes(lines, header=True):
    out = "住户号,评分\n" if header else ""
    out += "\n".join(f"{h},{s}" for h, s in lines)
    return out.encode("utf-8")


def csv3_bytes(lines, header=True, blank_batch=False):
    out = "住户号,评分,发卷批号\n" if header else ""
    out += "\n".join(
        f"{h},{s},{'' if blank_batch else b}" for h, s, b in lines
    )
    return out.encode("utf-8")


def _meta_int_helper(conn, key):
    return int(conn.execute(
        "SELECT value FROM meta WHERE key=?", (key,)
    ).fetchone()["value"])


class CsvParseTests(unittest.TestCase):
    def test_valid_with_and_without_header(self):
        rows = parse_scores(csv_bytes([("A", 0), ("B", 10)], header=True))
        self.assertEqual(rows, [("A", 0, DEFAULT_BATCH),
                                ("B", 10, DEFAULT_BATCH)])
        rows2 = parse_scores(csv_bytes([("A", 0), ("B", 10)], header=False))
        self.assertEqual(rows2, [("A", 0, DEFAULT_BATCH),
                                 ("B", 10, DEFAULT_BATCH)])

    def test_three_column_with_batch_ids(self):
        raw = csv3_bytes([("A", 1, "B1"), ("B", 9, "B2"), ("C", 4, "B1")])
        self.assertEqual(
            parse_scores(raw),
            [("A", 1, "B1"), ("B", 9, "B2"), ("C", 4, "B1")],
        )

    def test_blank_batch_goes_to_default(self):
        raw = csv3_bytes([("A", 1, "B1"), ("B", 9, "")])
        self.assertEqual(
            parse_scores(raw),
            [("A", 1, "B1"), ("B", 9, DEFAULT_BATCH)],
        )

    def test_three_column_header_skipped_even_though_it_has_three_cells(self):
        raw = "住户号,评分,发卷批号\nH1,7,B1\nH2,8,B2\n".encode("utf-8")
        self.assertEqual(parse_scores(raw),
                         [("H1", 7, "B1"), ("H2", 8, "B2")])

    def test_mixed_width_rejected(self):
        raw = "H1,7,B1\nH2,8\n".encode("utf-8")
        with self.assertRaises(CSVValidationError):
            parse_scores(raw)
        raw2 = "H1,7\nH2,8,B1\n".encode("utf-8")
        with self.assertRaises(CSVValidationError):
            parse_scores(raw2)

    def test_rejections(self):
        bad = [
            "住户号,评分\nA,11",          # out of range
            "住户号,评分\nA,x",           # non-integer
            "住户号,评分\nA",             # one column
            "住户号,评分\nA,5\nA,6",     # duplicate
            "住户号,评分\n ,5",           # empty id
            "住户号,评分",                # no records
            "H,1,B1,EXTRA",               # four columns
            "H,1,B\x00X",                 # NUL in batch id
        ]
        for text in bad:
            with self.assertRaises(CSVValidationError):
                parse_scores(text.encode("utf-8"))

    def test_bom_tolerated(self):
        raw = b"\xef\xbb\xbfhousehold,score\nH1,7\nH2,8\n"
        self.assertEqual(parse_scores(raw),
                         [("H1", 7, DEFAULT_BATCH),
                          ("H2", 8, DEFAULT_BATCH)])


class StorageAndAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.rows = [(f"H{i:03d}", (i * 3) % 11,
                      "B1" if i % 2 == 0 else "B2") for i in range(30)]
        replace_original(self.conn, self.rows, "2026-11-24T00:00:00+00:00")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_schema_and_initial_forward_direction(self):
        dirs = {r["batch_id"]: r["direction"]
                for r in self.conn.execute("SELECT batch_id, direction FROM batches")}
        self.assertEqual(dirs, {"B1": DIRECTION_FORWARD,
                                "B2": DIRECTION_FORWARD})
        # original table keeps raw entered scores
        stored = self.conn.execute(
            "SELECT household_id, raw_score FROM original_scores ORDER BY household_id"
        ).fetchall()
        self.assertEqual([(r["household_id"], r["raw_score"]) for r in stored],
                         sorted((h, s) for h, s, _ in self.rows))

    def test_original_table_kept_as_is(self):
        before = self.conn.execute(
            "SELECT household_id, raw_score, batch_id FROM original_scores ORDER BY household_id"
        ).fetchall()
        apply_correction(self.conn, "H000", "update", 10)
        apply_correction(self.conn, "H001", "remove", None)
        set_batch_direction(self.conn, "B1", DIRECTION_REVERSE)
        after = self.conn.execute(
            "SELECT household_id, raw_score, batch_id FROM original_scores ORDER BY household_id"
        ).fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])

    def test_summaries_match_household_level_computation(self):
        ids = [h for h, _, _ in self.rows]
        weights = {h: household_weights(h) for h in ids}
        score_map = {h: s for h, s, _ in self.rows}
        batch_map = {h: b for h, _, b in self.rows}
        for rnd in range(ROUNDS):
            for batch in ("B1", "B2"):
                members = [h for h in ids if batch_map[h] == batch]
                exp_w = sum(weights[h][rnd] for h in members)
                exp_ws = sum(weights[h][rnd] * score_map[h] for h in members)
                row = self.conn.execute(
                    "SELECT weight_total, weighted_sum FROM orig_rounds "
                    "WHERE round_index=? AND batch_id=?", (rnd, batch)
                ).fetchone()
                self.assertEqual((row["weight_total"], row["weighted_sum"]),
                                 (exp_w, exp_ws))

    def test_correction_only_changes_that_household(self):
        before = {
            (r["round_index"], r["bucket"]):
                (r["weight_total"], r["weighted_sum"])
            for r in self.conn.execute(
                "SELECT round_index, bucket, weight_total, weighted_sum FROM corr_rounds")
        }
        apply_correction(self.conn, "H005", "update", 10)
        old_score = dict((h, s) for h, s, _ in self.rows)["H005"]
        w5 = household_weights("H005")
        after = {
            (r["round_index"], r["bucket"]):
                (r["weight_total"], r["weighted_sum"])
            for r in self.conn.execute(
                "SELECT round_index, bucket, weight_total, weighted_sum FROM corr_rounds")
        }
        # the household's old batch bucket loses its contribution ...
        old_batch = dict((h, b) for h, _, b in self.rows)["H005"]
        for rnd in range(ROUNDS):
            self.assertEqual(
                after[(rnd, old_batch)],
                (before[(rnd, old_batch)][0] - w5[rnd],
                 before[(rnd, old_batch)][1] - w5[rnd] * old_score),
            )
            # ... and the manual bucket gains the new forward score
            self.assertEqual(
                after[(rnd, MANUAL_BATCH)],
                (before.get((rnd, MANUAL_BATCH), (0, 0))[0] + w5[rnd],
                 before.get((rnd, MANUAL_BATCH), (0, 0))[1] + w5[rnd] * 10),
            )
        detail = self.conn.execute(
            "SELECT manual FROM corrected_scores WHERE household_id='H005'"
        ).fetchone()
        self.assertEqual(detail["manual"], 1)
        # original summary never changes
        orig = {
            (r["round_index"], r["batch_id"]):
                (r["weight_total"], r["weighted_sum"])
            for r in self.conn.execute(
                "SELECT round_index, batch_id, weight_total, weighted_sum FROM orig_rounds")
        }
        for rnd in range(ROUNDS):
            for b in ("B1", "B2"):
                self.assertIn((rnd, b), orig)

    def test_same_value_edit_still_counts_as_manual(self):
        current = dict((h, s) for h, s, _ in self.rows)["H000"]
        apply_correction(self.conn, "H000", "update", current)
        row = self.conn.execute(
            "SELECT manual FROM corrected_scores WHERE household_id='H000'"
        ).fetchone()
        self.assertEqual(row["manual"], 1)
        # its contribution now lives in the manual bucket
        in_batch = self.conn.execute(
            "SELECT COUNT(*) AS k FROM corr_rounds WHERE bucket='B1'"
        ).fetchone()["k"]
        in_manual = self.conn.execute(
            "SELECT COUNT(*) AS k FROM corr_rounds WHERE bucket=?",
            (MANUAL_BATCH,),
        ).fetchone()["k"]
        self.assertEqual(in_batch, ROUNDS)
        self.assertEqual(in_manual, ROUNDS)

    def test_add_and_remove_household(self):
        apply_correction(self.conn, "NEW1", "add", 9)
        self.assertEqual(get_household(self.conn, "NEW1"),
                         {"household_id": "NEW1",
                          "raw_score": None,
                          "original_score": None,
                          "corrected_score": 9,
                          "batch_id": MANUAL_BATCH,
                          "direction": None,
                          "manual": True,
                          "removed": False})
        n = int(self.conn.execute(
            f"SELECT value FROM meta WHERE key='{META_CORR_N}'").fetchone()["value"])
        self.assertEqual(n, 31)

        apply_correction(self.conn, "NEW1", "remove", None)
        self.assertIsNone(get_household(self.conn, "NEW1"))
        n = int(self.conn.execute(
            f"SELECT value FROM meta WHERE key='{META_CORR_N}'").fetchone()["value"])
        self.assertEqual(n, 30)

    def test_failed_correction_rolls_back_keeping_previous_result(self):
        apply_correction(self.conn, "H000", "update", 4)
        snap = {
            (r["round_index"], r["bucket"]):
                (r["weight_total"], r["weighted_sum"])
            for r in self.conn.execute(
                "SELECT round_index, bucket, weight_total, weighted_sum FROM corr_rounds")
        }
        with self.assertRaises(ValueError):
            apply_correction(self.conn, "H000", "add", 5)       # already exists
        with self.assertRaises(ValueError):
            apply_correction(self.conn, "GHOST", "update", 5)   # missing
        with self.assertRaises(ValueError):
            apply_correction(self.conn, "H001", "update", 99)   # out of range
        after = {
            (r["round_index"], r["bucket"]):
                (r["weight_total"], r["weighted_sum"])
            for r in self.conn.execute(
                "SELECT round_index, bucket, weight_total, weighted_sum FROM corr_rounds")
        }
        self.assertEqual(snap, after)
        score = self.conn.execute(
            "SELECT score FROM corrected_scores WHERE household_id='H000'"
        ).fetchone()["score"]
        self.assertEqual(score, 4)

    def test_reset_follows_current_directions(self):
        set_batch_direction(self.conn, "B1", DIRECTION_REVERSE)
        apply_correction(self.conn, "H000", "update", 10)
        apply_correction(self.conn, "H001", "remove", None)
        apply_correction(self.conn, "NEW9", "add", 3)
        reset_corrected(self.conn)
        rows = self.conn.execute(
            "SELECT c.household_id, c.score, c.manual, o.raw_score, o.batch_id "
            "FROM corrected_scores c JOIN original_scores o "
            "ON o.household_id = c.household_id"
        ).fetchall()
        self.assertEqual(len(rows), 30)  # supplementary household gone
        dirs = {r["batch_id"]: r["direction"]
                for r in self.conn.execute(
                    "SELECT batch_id, direction FROM batches")}
        for r in rows:
            self.assertEqual(r["manual"], 0)
            expected = (10 - r["raw_score"]
                        if dirs[r["batch_id"]] == DIRECTION_REVERSE
                        else r["raw_score"])
            self.assertEqual(r["score"], expected)
        # no manual bucket remains after reset
        k = self.conn.execute(
            "SELECT COUNT(*) AS k FROM corr_rounds WHERE bucket=?",
            (MANUAL_BATCH,),
        ).fetchone()["k"]
        self.assertEqual(k, 0)


class FlakyConnection:
    """Proxy that fails one specific SQL statement to exercise rollback."""

    def __init__(self, real_conn, fail_prefix, error):
        self._real = real_conn
        self._fail_prefix = fail_prefix
        self._error = error

    def execute(self, sql, params=()):
        if sql.lstrip().startswith(self._fail_prefix):
            raise self._error
        return self._real.execute(sql, params)

    def executemany(self, sql, params):
        return self._real.executemany(sql, params)

    def executescript(self, sql):
        return self._real.executescript(sql)

    def rollback(self):
        return self._real.rollback()

    def commit(self):
        return self._real.commit()

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


class BatchDirectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "dir.db")
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.rows = [("A", 2, "B1"), ("B", 4, "B1"),
                     ("C", 6, "B2"), ("D", 8, DEFAULT_BATCH)]
        replace_original(self.conn, self.rows, "t")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_flip_converts_both_summaries_but_keeps_raw_detail(self):
        outcome = set_batch_direction(self.conn, "B1", DIRECTION_REVERSE)
        self.assertTrue(outcome["changed"])
        self.assertEqual(outcome["households"], 2)

        # raw scores remain exactly as entered
        raw = dict((r["household_id"], r["raw_score"])
                   for r in self.conn.execute(
                       "SELECT household_id, raw_score FROM original_scores"))
        self.assertEqual(raw, {"A": 2, "B": 4, "C": 6, "D": 8})

        # unweighted sums: B1 2+4=6 -> 8+6=14, so orig total 20 -> 28
        orig_sum = int(self.conn.execute(
            f"SELECT value FROM meta WHERE key='{META_ORIG_SUM}'"
        ).fetchone()["value"])
        self.assertEqual(orig_sum, 28)
        corr_sum = int(self.conn.execute(
            f"SELECT value FROM meta WHERE key='{META_CORR_SUM}'"
        ).fetchone()["value"])
        self.assertEqual(corr_sum, 28)

        # per-round rows match an independent household-level recomputation
        for rnd in range(ROUNDS):
            wa = household_weights("A")[rnd]
            wb = household_weights("B")[rnd]
            exp_ws = wa * 8 + wb * 6
            exp_w = wa + wb
            row = self.conn.execute(
                "SELECT weight_total, weighted_sum FROM orig_rounds "
                "WHERE round_index=? AND batch_id='B1'", (rnd,)
            ).fetchone()
            self.assertEqual((row["weight_total"], row["weighted_sum"]),
                             (exp_w, exp_ws))
            crow = self.conn.execute(
                "SELECT weight_total, weighted_sum FROM corr_rounds "
                "WHERE round_index=? AND bucket='B1'", (rnd,)
            ).fetchone()
            self.assertEqual((crow["weight_total"], crow["weighted_sum"]),
                             (exp_w, exp_ws))

        # batch_summary converted
        summ = self.conn.execute(
            "SELECT count, score_sum FROM batch_summary "
            "WHERE table_name='orig' AND bucket='B1'"
        ).fetchone()
        self.assertEqual((summ["count"], summ["score_sum"]), (2, 14))

    def test_flip_back_restores_everything(self):
        set_batch_direction(self.conn, "B1", DIRECTION_REVERSE)
        back = set_batch_direction(self.conn, "B1", DIRECTION_FORWARD)
        self.assertTrue(back["changed"])
        orig_sum = int(self.conn.execute(
            f"SELECT value FROM meta WHERE key='{META_ORIG_SUM}'"
        ).fetchone()["value"])
        self.assertEqual(orig_sum, 20)  # 2+4+6+8
        res = analyze(self.conn)
        self.assertEqual(res["unweighted_diff"], 0.0)
        self.assertEqual(res["p025"], 0.0)
        self.assertEqual(res["p975"], 0.0)

    def test_idempotent_same_direction_is_noop(self):
        out = set_batch_direction(self.conn, "B2", DIRECTION_FORWARD)
        self.assertFalse(out["changed"])

    def test_unknown_batch_and_bad_direction_rejected(self):
        with self.assertRaises(ValueError):
            set_batch_direction(self.conn, "NOPE", DIRECTION_REVERSE)
        with self.assertRaises(ValueError):
            set_batch_direction(self.conn, "B1", 7)

    def test_failed_flip_rolls_back_keeping_old_caliber(self):
        flaky = FlakyConnection(
            self.conn, "UPDATE batches", RuntimeError("simulated failure")
        )
        with self.assertRaises(RuntimeError):
            set_batch_direction(flaky, "B1", DIRECTION_REVERSE)
        # direction still forward, totals untouched
        direction = self.conn.execute(
            "SELECT direction FROM batches WHERE batch_id='B1'"
        ).fetchone()["direction"]
        self.assertEqual(direction, DIRECTION_FORWARD)
        orig_sum = int(self.conn.execute(
            f"SELECT value FROM meta WHERE key='{META_ORIG_SUM}'"
        ).fetchone()["value"])
        self.assertEqual(orig_sum, 20)
        # summaries still internally consistent -> analysis still works
        res = analyze(self.conn)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["unweighted_diff"], 0.0)

    def test_manual_scores_and_removals_survive_flips(self):
        # manual edit (same value counts too), supplementary add, removal
        apply_correction(self.conn, "A", "update", 9)       # B1 household
        apply_correction(self.conn, "EXTRA", "add", 5)
        apply_correction(self.conn, "C", "remove", None)    # B2 household
        set_batch_direction(self.conn, "B1", DIRECTION_REVERSE)
        set_batch_direction(self.conn, "B1", DIRECTION_FORWARD)
        set_batch_direction(self.conn, "B2", DIRECTION_REVERSE)

        a = get_household(self.conn, "A")
        self.assertEqual(a["corrected_score"], 9)
        self.assertTrue(a["manual"])
        extra = get_household(self.conn, "EXTRA")
        self.assertEqual(extra["corrected_score"], 5)
        c = get_household(self.conn, "C")
        self.assertIsNone(c["corrected_score"])
        self.assertTrue(c["removed"])
        # manual bucket totals are stable across all flips
        manual = self.conn.execute(
            "SELECT count, score_sum FROM batch_summary "
            "WHERE table_name='corr' AND bucket=?",
            (MANUAL_BATCH,),
        ).fetchone()
        self.assertEqual((manual["count"], manual["score_sum"]), (2, 14))

    def test_lookup_shows_raw_converted_and_corrected(self):
        set_batch_direction(self.conn, "B1", DIRECTION_REVERSE)
        info = get_household(self.conn, "A")
        self.assertEqual(info["raw_score"], 2)
        self.assertEqual(info["original_score"], 8)       # 10 - 2
        self.assertEqual(info["corrected_score"], 8)      # follows batch
        self.assertEqual(info["direction"], DIRECTION_REVERSE)

        apply_correction(self.conn, "A", "update", 3)
        info2 = get_household(self.conn, "A")
        self.assertEqual(info2["raw_score"], 2)
        self.assertEqual(info2["original_score"], 8)
        self.assertEqual(info2["corrected_score"], 3)     # manual forward value
        self.assertTrue(info2["manual"])

    def test_analysis_reflects_flip_and_manual_edits(self):
        set_batch_direction(self.conn, "B1", DIRECTION_REVERSE)
        res = analyze(self.conn)
        self.assertEqual(res["status"], "ok")
        # four households: rounds where every Pois(1) weight is zero are skipped
        expected_valid = sum(
            1 for rnd in range(ROUNDS)
            if sum(household_weights(h)[rnd] for h, _, _ in self.rows) > 0
        )
        self.assertEqual(res["valid_rounds"], expected_valid)
        # both tables converted identically -> diff zero
        self.assertEqual(res["unweighted_diff"], 0.0)

        apply_correction(self.conn, "A", "update", 10)
        res2 = analyze(self.conn)
        # corrected: A=10 (manual), B=6, C=6, D=8 -> mean 30/4 = 7.5
        # original: 8+6+6+8 = 28 -> mean 7
        self.assertAlmostEqual(res2["corr_mean"], 7.5)
        self.assertAlmostEqual(res2["orig_mean"], 7.0)
        self.assertAlmostEqual(res2["unweighted_diff"], 0.5)

    def test_list_batches_reports_directions_and_counts(self):
        apply_correction(self.conn, "A", "update", 1)
        batches = {b["batch_id"]: b for b in list_batches(self.conn)}
        self.assertEqual(batches["B1"]["direction"], DIRECTION_FORWARD)
        self.assertEqual(batches["B1"]["orig_count"], 2)
        self.assertEqual(batches["B1"]["following_count"], 1)  # A moved out
        self.assertEqual(batches[MANUAL_BATCH]["following_count"], 1)

    def test_flip_uses_only_summaries_and_never_touches_detail_tables(self):
        def dump_detail():
            return (
                self.conn.execute(
                    "SELECT household_id, raw_score, batch_id "
                    "FROM original_scores ORDER BY household_id"
                ).fetchall(),
                self.conn.execute(
                    "SELECT household_id, score, manual, batch_id "
                    "FROM corrected_scores ORDER BY household_id"
                ).fetchall(),
            )

        before = dump_detail()
        set_batch_direction(self.conn, "B1", DIRECTION_REVERSE)
        set_batch_direction(self.conn, "B2", DIRECTION_REVERSE)
        set_batch_direction(self.conn, "B1", DIRECTION_FORWARD)
        after = dump_detail()
        self.assertEqual([tuple(r) for r in before[0]],
                         [tuple(r) for r in after[0]])
        self.assertEqual([tuple(r) for r in before[1]],
                         [tuple(r) for r in after[1]])

    def test_summaries_match_independent_recompute_after_mixed_operations(self):
        # flip, manual edits (incl. same-value), supplementary add, removal
        set_batch_direction(self.conn, "B1", DIRECTION_REVERSE)
        apply_correction(self.conn, "A", "update", 9)
        apply_correction(self.conn, "D", "update", 8)   # same value -> still manual
        apply_correction(self.conn, "EXTRA", "add", 2)
        apply_correction(self.conn, "C", "remove", None)
        set_batch_direction(self.conn, "B2", DIRECTION_REVERSE)
        set_batch_direction(self.conn, "B1", DIRECTION_FORWARD)

        directions = {r["batch_id"]: r["direction"]
                      for r in self.conn.execute(
                          "SELECT batch_id, direction FROM batches")}

        # --- independent recomputation from the detail tables -------------
        orig_detail = self.conn.execute(
            "SELECT household_id, raw_score, batch_id FROM original_scores"
        ).fetchall()
        corr_detail = self.conn.execute(
            "SELECT household_id, score, manual, batch_id FROM corrected_scores"
        ).fetchall()

        exp_orig: dict[tuple[int, str], list[int]] = {}
        exp_corr: dict[tuple[int, str], list[int]] = {}
        for rnd in range(ROUNDS):
            for b in directions:
                exp_orig[(rnd, b)] = [0, 0]
            exp_corr[(rnd, MANUAL_BATCH)] = [0, 0]

        orig_total = 0
        for r in orig_detail:
            score = (10 - r["raw_score"]
                     if directions[r["batch_id"]] == DIRECTION_REVERSE
                     else r["raw_score"])
            orig_total += score
            weights = household_weights(r["household_id"])
            for rnd, w in enumerate(weights):
                cell = exp_orig[(rnd, r["batch_id"])]
                cell[0] += w
                cell[1] += w * score

        corr_total = 0
        for r in corr_detail:
            if r["manual"]:
                score = r["score"]
                bucket = MANUAL_BATCH
            else:
                score = (10 - r["score"]
                         if directions[r["batch_id"]] == DIRECTION_REVERSE
                         else r["score"])
                bucket = r["batch_id"]
            corr_total += score
            weights = household_weights(r["household_id"])
            for rnd, w in enumerate(weights):
                cell = exp_corr.setdefault((rnd, bucket), [0, 0])
                cell[0] += w
                cell[1] += w * score

        for (rnd, bucket), (w, ws) in exp_orig.items():
            row = self.conn.execute(
                "SELECT weight_total, weighted_sum FROM orig_rounds "
                "WHERE round_index=? AND batch_id=?", (rnd, bucket)
            ).fetchone()
            self.assertEqual((row["weight_total"], row["weighted_sum"]), (w, ws))
        for (rnd, bucket), (w, ws) in exp_corr.items():
            row = self.conn.execute(
                "SELECT weight_total, weighted_sum FROM corr_rounds "
                "WHERE round_index=? AND bucket=?", (rnd, bucket)
            ).fetchone()
            self.assertEqual((row["weight_total"], row["weighted_sum"]), (w, ws))

        self.assertEqual(_meta_int_helper(self.conn, META_ORIG_SUM), orig_total)
        self.assertEqual(_meta_int_helper(self.conn, META_CORR_SUM), corr_total)
        self.assertEqual(_meta_int_helper(self.conn, META_CORR_N),
                         len(corr_detail))
        # analysis must stay internally consistent
        res = analyze(self.conn)
        self.assertEqual(res["status"], "ok")
        self.assertAlmostEqual(
            res["orig_mean"],
            orig_total / _meta_int_helper(self.conn, META_ORIG_N),
        )
        self.assertAlmostEqual(
            res["corr_mean"],
            corr_total / len(corr_detail),
        )


class LegacyDatabaseMigrationTests(unittest.TestCase):
    """A v1 database (two-column world) must open and behave as before."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "legacy.db")
        self.rows = [("A", 3), ("B", 5), ("C", 7)]
        self._build_v1_database()

    def tearDown(self):
        self.tmp.cleanup()

    def _build_v1_database(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE original_scores (
                household_id TEXT PRIMARY KEY,
                score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 10)
            );
            CREATE TABLE corrected_scores (
                household_id TEXT PRIMARY KEY,
                score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 10)
            );
            CREATE TABLE orig_rounds (
                round_index INTEGER PRIMARY KEY CHECK (round_index BETWEEN 0 AND 999),
                weight_total INTEGER NOT NULL,
                weighted_sum INTEGER NOT NULL
            );
            CREATE TABLE corr_rounds (
                round_index INTEGER PRIMARY KEY CHECK (round_index BETWEEN 0 AND 999),
                weight_total INTEGER NOT NULL,
                weighted_sum INTEGER NOT NULL
            );
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        conn.executemany(
            "INSERT INTO original_scores(household_id, score) VALUES(?, ?)", self.rows
        )
        conn.executemany(
            "INSERT INTO corrected_scores(household_id, score) VALUES(?, ?)", self.rows
        )
        for rnd in range(ROUNDS):
            ow = ows = cw = cws = 0
            for hid, score in self.rows:
                w = household_weights(hid)[rnd]
                ow += w
                ows += w * score
                cw += w
                cws += w * score
            conn.execute(
                "INSERT INTO orig_rounds VALUES(?, ?, ?)", (rnd, ow, ows)
            )
            conn.execute(
                "INSERT INTO corr_rounds VALUES(?, ?, ?)", (rnd, cw, cws)
            )
        total = sum(s for _, s in self.rows)
        conn.execute("INSERT INTO meta VALUES('orig_score_sum', ?)", (str(total),))
        conn.execute("INSERT INTO meta VALUES('orig_count', '3')")
        conn.execute("INSERT INTO meta VALUES('corr_score_sum', ?)", (str(total),))
        conn.execute("INSERT INTO meta VALUES('corr_count', '3')")
        conn.execute("INSERT INTO meta VALUES('uploaded_at', '2026-11-23T00:00:00+00:00')")
        conn.commit()
        conn.close()

    def test_migrates_and_keeps_forward_behaviour(self):
        conn = connect(self.db_path)
        init_db(conn)
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        self.assertEqual(version, "2")
        direction = conn.execute(
            "SELECT direction FROM batches WHERE batch_id=?", (DEFAULT_BATCH,)
        ).fetchone()["direction"]
        self.assertEqual(direction, DIRECTION_FORWARD)
        res = analyze(conn)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["orig_n"], 3)
        self.assertAlmostEqual(res["orig_mean"], 5.0)
        self.assertEqual(res["unweighted_diff"], 0.0)
        expected_valid = sum(
            1 for rnd in range(ROUNDS)
            if sum(household_weights(h)[rnd] for h, _ in self.rows) > 0
        )
        self.assertEqual(res["valid_rounds"], expected_valid)

        # migrated round summaries match household-level recomputation
        for rnd in range(ROUNDS):
            exp_w = sum(household_weights(h)[rnd] for h, _ in self.rows)
            exp_ws = sum(household_weights(h)[rnd] * s
                         for h, s in self.rows)
            row = conn.execute(
                "SELECT weight_total, weighted_sum FROM orig_rounds "
                "WHERE round_index=? AND batch_id=?", (rnd, DEFAULT_BATCH)
            ).fetchone()
            self.assertEqual((row["weight_total"], row["weighted_sum"]),
                             (exp_w, exp_ws))

        # the legacy default batch can now be flipped like any other batch
        set_batch_direction(conn, DEFAULT_BATCH, DIRECTION_REVERSE)
        info = get_household(conn, "A")
        self.assertEqual(info["raw_score"], 3)
        self.assertEqual(info["original_score"], 7)
        self.assertEqual(info["corrected_score"], 7)
        conn.close()

    def test_legacy_manual_edits_migrate_into_manual_bucket(self):
        # tamper the legacy corrected table: A edited 3 -> 9, B removed,
        # and SUP is a supplementary household with no original row
        conn0 = sqlite3.connect(self.db_path)
        conn0.execute("UPDATE corrected_scores SET score=9 WHERE household_id='A'")
        conn0.execute("DELETE FROM corrected_scores WHERE household_id='B'")
        conn0.execute(
            "INSERT INTO corrected_scores(household_id, score) VALUES('SUP', 4)"
        )
        conn0.commit()
        conn0.close()

        conn = connect(self.db_path)
        init_db(conn)
        a = conn.execute(
            "SELECT manual, score FROM corrected_scores WHERE household_id='A'"
        ).fetchone()
        self.assertEqual((a["manual"], a["score"]), (1, 9))
        sup = conn.execute(
            "SELECT manual, score, batch_id FROM corrected_scores "
            "WHERE household_id='SUP'"
        ).fetchone()
        self.assertEqual((sup["manual"], sup["score"]), (1, 4))
        self.assertEqual(sup["batch_id"], DEFAULT_BATCH)
        b = conn.execute(
            "SELECT 1 FROM corrected_scores WHERE household_id='B'"
        ).fetchone()
        self.assertIsNone(b)
        manual = conn.execute(
            "SELECT COUNT(*) AS k FROM corr_rounds WHERE bucket=?",
            (MANUAL_BATCH,),
        ).fetchone()["k"]
        self.assertEqual(manual, ROUNDS)
        # the manual bucket pools both legacy manual households (A and SUP)
        for rnd in range(ROUNDS):
            exp_w = household_weights("A")[rnd] + household_weights("SUP")[rnd]
            exp_ws = (household_weights("A")[rnd] * 9
                      + household_weights("SUP")[rnd] * 4)
            row = conn.execute(
                "SELECT weight_total, weighted_sum FROM corr_rounds "
                "WHERE round_index=? AND bucket=?", (rnd, MANUAL_BATCH)
            ).fetchone()
            self.assertEqual((row["weight_total"], row["weighted_sum"]),
                             (exp_w, exp_ws))
        # flipping the default batch leaves the migrated manual scores alone
        set_batch_direction(conn, DEFAULT_BATCH, DIRECTION_REVERSE)
        info = get_household(conn, "A")
        self.assertEqual(info["corrected_score"], 9)
        self.assertEqual(info["original_score"], 7)  # 10 - 3
        sup_info = get_household(conn, "SUP")
        self.assertEqual(sup_info["corrected_score"], 4)
        self.assertIsNone(sup_info["original_score"])
        conn.close()


class FlaskFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "web.db")
        self.app = create_app(db_path=self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _upload(self, rows, header=True, name="survey.csv"):
        data = {
            "csv_file": (io.BytesIO(csv_bytes(rows, header=header)), name),
        }
        return self.client.post(
            "/upload", data=data, content_type="multipart/form-data",
            follow_redirects=True,
        )

    def _upload3(self, rows, header=True):
        data = {
            "csv_file": (io.BytesIO(csv3_bytes(rows, header=header)), "s3.csv"),
        }
        return self.client.post(
            "/upload", data=data, content_type="multipart/form-data",
            follow_redirects=True,
        )

    def test_full_key_operations(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("尚未上传原表", html)

        rows = [(f"H{i:02d}", (i * 7) % 11) for i in range(20)]
        resp = self._upload(rows)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("上传成功", resp.get_data(as_text=True))
        self.assertIn("1000 / 1000", resp.get_data(as_text=True))

        bad = self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(b"id,score\nX,99"), "bad.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        self.assertIn("上传被拒绝", bad.get_data(as_text=True))
        still = self.client.get("/").get_data(as_text=True)
        self.assertIn("1000 / 1000", still)

        r = self.client.post("/correct", data={
            "household_id": "H00", "action": "update", "score": "10",
        }, follow_redirects=True)
        self.assertIn("已对住户 H00 执行「改分」", r.get_data(as_text=True))

        r = self.client.post("/correct", data={
            "household_id": "H99", "action": "add", "score": "2",
        }, follow_redirects=True)
        self.assertIn("补录", r.get_data(as_text=True))

        r = self.client.get("/?household_id=H99")
        body = r.get_data(as_text=True)
        self.assertIn("H99", body)
        self.assertIn("原表无此户", body)

        r = self.client.post("/correct", data={
            "household_id": "H01", "action": "update", "score": "50",
        }, follow_redirects=True)
        self.assertIn("更正失败", r.get_data(as_text=True))

        r = self.client.post("/correct", data={
            "household_id": "H99", "action": "remove",
        }, follow_redirects=True)
        self.assertIn("移除", r.get_data(as_text=True))

        r = self.client.post("/reset", follow_redirects=True)
        self.assertIn("重置", r.get_data(as_text=True))
        self.assertIn("+0.0000", r.get_data(as_text=True))

        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_batch_flip_web_flow_with_three_column_csv(self):
        rows = [("A", 2, "B1"), ("B", 4, "B1"),
                ("C", 6, "B2"), ("D", 8, "")]
        r = self._upload3(rows)
        self.assertIn("3 个发卷批次", r.get_data(as_text=True))
        # B1 and the default batch both appear (3 distinct batch ids)
        body = r.get_data(as_text=True)
        self.assertIn("B1", body)
        self.assertIn("B2", body)

        # flip B1 to reverse
        r = self.client.post("/batch/direction", data={
            "batch_id": "B1", "direction": str(DIRECTION_REVERSE),
        }, follow_redirects=True)
        self.assertIn("已切换为反向", r.get_data(as_text=True))

        # lookup shows raw / converted / corrected
        body = self.client.get("/?household_id=A").get_data(as_text=True)
        self.assertIn("录入原分", body)
        self.assertIn(">2<", body)          # raw
        self.assertIn(">8<", body)          # converted 10-2 and corrected

        # analysis: both tables flipped identically -> diff 0
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("+0.0000", body)

        # manual edit survives a flip-back
        self.client.post("/correct", data={
            "household_id": "A", "action": "update", "score": "1",
        }, follow_redirects=True)
        self.client.post("/batch/direction", data={
            "batch_id": "B1", "direction": str(DIRECTION_FORWARD),
        }, follow_redirects=True)
        body = self.client.get("/?household_id=A").get_data(as_text=True)
        self.assertIn(">1<", body)  # manual score intact

        # unknown batch -> old caliber kept, error flash
        r = self.client.post("/batch/direction", data={
            "batch_id": "GHOST", "direction": str(DIRECTION_REVERSE),
        }, follow_redirects=True)
        self.assertIn("切换失败，已沿用旧口径", r.get_data(as_text=True))

        # reset follows the current (forward) caliber
        r = self.client.post("/reset", follow_redirects=True)
        self.assertIn("重置", r.get_data(as_text=True))
        body = self.client.get("/?household_id=A").get_data(as_text=True)
        self.assertIn(">2<", body)

    def test_all_removed_shows_reason_in_page(self):
        self._upload([("A", 3), ("B", 5)])
        for h in ("A", "B"):
            self.client.post("/correct", data={
                "household_id": h, "action": "remove",
            }, follow_redirects=True)
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("无法计算", body)
        self.assertIn("更正表已无任何住户", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
