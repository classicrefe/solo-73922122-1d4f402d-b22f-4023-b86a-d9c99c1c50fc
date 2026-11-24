"""Flask review application: upload, batch direction, corrections, analysis."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from flask import Flask, flash, redirect, render_template, request, url_for

from .csv_io import CSVValidationError, parse_scores
from .db import (
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    apply_correction,
    connect,
    get_household,
    has_dataset,
    init_db,
    list_batches,
    replace_original,
    reset_corrected,
    set_batch_direction,
)
from .stats import analyze


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
    app.config["DB_PATH"] = db_path or os.environ.get(
        "DB_PATH", os.path.join(os.getcwd(), "data", "survey.db")
    )
    os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)

    def get_conn():
        conn = connect(app.config["DB_PATH"])
        init_db(conn)
        return conn

    @app.route("/", methods=["GET"])
    def index():
        conn = get_conn()
        try:
            uploaded = has_dataset(conn)
            result = analyze(conn) if uploaded else None
            batches = list_batches(conn) if uploaded else []
            lookup = None
            lookup_id = request.args.get("household_id", "").strip()
            if uploaded and lookup_id:
                lookup = get_household(conn, lookup_id)
                if lookup is None:
                    flash(f"未找到住户 {lookup_id}（原表与更正表均无此户）。", "warn")
            return render_template(
                "index.html", result=result, uploaded=uploaded,
                lookup=lookup, lookup_id=lookup_id, batches=batches,
                DIRECTION_FORWARD=DIRECTION_FORWARD,
                DIRECTION_REVERSE=DIRECTION_REVERSE,
            )
        finally:
            conn.close()

    @app.route("/upload", methods=["POST"])
    def upload():
        file = request.files.get("csv_file")
        if file is None or file.filename == "":
            flash("请选择要上传的 CSV 文件。", "error")
            return redirect(url_for("index"))
        raw = file.read()
        try:
            rows = parse_scores(raw)
        except CSVValidationError as exc:
            flash(f"上传被拒绝，数据库未改动：{exc}", "error")
            return redirect(url_for("index"))

        conn = get_conn()
        try:
            replace_original(
                conn, rows,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        except Exception as exc:  # pragma: no cover - defensive rollback path
            conn.rollback()
            flash(f"入库失败，已保留上一结果：{exc}", "error")
            return redirect(url_for("index"))
        finally:
            conn.close()
        batch_count = len({batch_id for _, _, batch_id in rows})
        flash(
            f"上传成功：{len(rows)} 户、{batch_count} 个发卷批次已原样入库（初始均为正向），"
            "更正表已初始化为原表副本。",
            "ok",
        )
        return redirect(url_for("index"))

    @app.route("/batch/direction", methods=["POST"])
    def batch_direction():
        batch_id = request.form.get("batch_id", "")
        direction_text = request.form.get("direction", "").strip()
        conn = get_conn()
        try:
            if not has_dataset(conn):
                flash("尚未上传原表，无法切换批次方向。", "error")
                return redirect(url_for("index"))
            try:
                direction = int(direction_text)
            except ValueError:
                raise ValueError("方向参数必须是 0（正向）或 1（反向）")
            outcome = set_batch_direction(conn, batch_id, direction)
        except ValueError as exc:
            conn.rollback()
            flash(f"切换失败，已沿用旧口径：{exc}", "error")
            return redirect(url_for("index"))
        except Exception as exc:  # pragma: no cover - defensive rollback path
            conn.rollback()
            flash(f"切换失败，已沿用旧口径：{exc}", "error")
            return redirect(url_for("index"))
        finally:
            conn.close()

        label = "反向（10 减原分）" if direction == DIRECTION_REVERSE else "正向（原分）"
        shown_id = batch_id if batch_id else "默认批次（两列 CSV / 空批号）"
        if not outcome["changed"]:
            flash(f"批次 {shown_id} 当前已是{label}，口径未变化。", "warn")
        else:
            flash(
                f"批次 {shown_id} 已切换为{label}：两表汇总已同事务一同生效；"
                "人工改分、补录与移除状态均已保留。",
                "ok",
            )
        return redirect(url_for("index"))

    @app.route("/correct", methods=["POST"])
    def correct():
        household_id = request.form.get("household_id", "").strip()
        action = request.form.get("action", "").strip()
        score_text = request.form.get("score", "").strip()
        if not household_id:
            flash("请填写住户号。", "error")
            return redirect(url_for("index"))

        new_score: int | None = None
        if action != "remove":
            try:
                new_score = int(score_text)
            except ValueError:
                flash("评分必须是 0～10 的整数（正向分值）。", "error")
                return redirect(url_for("index"))

        conn = get_conn()
        try:
            if not has_dataset(conn):
                flash("尚未上传原表，无法更正。", "error")
                return redirect(url_for("index"))
            apply_correction(conn, household_id, action, new_score)
        except ValueError as exc:
            conn.rollback()
            flash(f"更正失败，已保留上一结果：{exc}", "error")
            return redirect(url_for("index"))
        except Exception as exc:  # pragma: no cover - defensive rollback path
            conn.rollback()
            flash(f"更正失败，已保留上一结果：{exc}", "error")
            return redirect(url_for("index"))
        finally:
            conn.close()

        verb = {"add": "补录", "update": "改分", "remove": "移除"}.get(action, action)
        flash(
            f"已对住户 {household_id} 执行「{verb}」（按正向分值记录，同分改分也算人工改分），"
            "更正表与汇总已一同生效。",
            "ok",
        )
        return redirect(url_for("index"))

    @app.route("/reset", methods=["POST"])
    def reset():
        conn = get_conn()
        try:
            if not has_dataset(conn):
                flash("尚未上传原表，无需重置。", "warn")
                return redirect(url_for("index"))
            reset_corrected(conn)
        except Exception as exc:  # pragma: no cover - defensive rollback path
            conn.rollback()
            flash(f"重置失败，已保留上一结果：{exc}", "error")
            return redirect(url_for("index"))
        finally:
            conn.close()
        flash("更正表已按当前原表口径（含各批次方向）重新跟随，汇总已同步重建。", "ok")
        return redirect(url_for("index"))

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
