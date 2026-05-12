#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评分数据库模块
负责持久化评分记录、同步人工修正分数，并导出 CSV。
"""

import csv
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any


class ScoringDatabase:
    """本地 SQLite 评分记录库。"""

    COLUMNS = [
        "id",
        "session_id",
        "record_index",
        "question_index",
        "mode",
        "provider",
        "model",
        "base_url",
        "ai_score",
        "manual_score",
        "status",
        "criteria",
        "ai_response",
        "image_path",
        "created_at",
        "updated_at",
    ]

    CSV_HEADERS = {
        "id": "ID",
        "session_id": "会话ID",
        "record_index": "记录序号",
        "question_index": "题号",
        "mode": "模式",
        "provider": "服务商",
        "model": "模型",
        "base_url": "Base URL",
        "ai_score": "AI分数",
        "manual_score": "人工分数",
        "status": "状态",
        "criteria": "评分标准",
        "ai_response": "AI响应",
        "image_path": "截图路径",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    }

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _init_db(self):
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scoring_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    record_index INTEGER NOT NULL,
                    question_index TEXT,
                    mode TEXT NOT NULL,
                    provider TEXT,
                    model TEXT,
                    base_url TEXT,
                    ai_score REAL NOT NULL,
                    manual_score REAL,
                    status TEXT NOT NULL,
                    criteria TEXT,
                    ai_response TEXT,
                    image_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scoring_records_session_record
                ON scoring_records(session_id, record_index)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scoring_records_created_at
                ON scoring_records(created_at)
                """
            )

    def insert_record(self, **record: Any) -> int:
        now = self._now()
        values = {
            "session_id": record.get("session_id", ""),
            "record_index": record.get("record_index"),
            "question_index": record.get("question_index"),
            "mode": record.get("mode", "single"),
            "provider": record.get("provider", ""),
            "model": record.get("model", ""),
            "base_url": record.get("base_url", ""),
            "ai_score": record.get("ai_score"),
            "manual_score": record.get("manual_score"),
            "status": record.get("status", "待标记"),
            "criteria": record.get("criteria", ""),
            "ai_response": record.get("ai_response", ""),
            "image_path": record.get("image_path", ""),
            "created_at": now,
            "updated_at": now,
        }
        columns = list(values.keys())
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO scoring_records ({', '.join(columns)}) VALUES ({placeholders})"
        with self._connection() as conn:
            cur = conn.execute(sql, [values[col] for col in columns])
            return int(cur.lastrowid)

    def update_manual_score(self, record_id: int, manual_score: int | float | None, status: str):
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE scoring_records
                SET manual_score = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (manual_score, status, self._now(), record_id),
            )

    def get_stats(self) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN manual_score IS NOT NULL THEN 1 ELSE 0 END) AS marked,
                    SUM(CASE WHEN status = '✗ 偏差' THEN 1 ELSE 0 END) AS mismatches,
                    AVG(ai_score) AS avg_ai_score
                FROM scoring_records
                """
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "marked": int(row["marked"] or 0),
            "mismatches": int(row["mismatches"] or 0),
            "avg_ai_score": float(row["avg_ai_score"] or 0.0),
        }

    def export_csv(self, output_path: str | Path) -> int:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(self.COLUMNS)} FROM scoring_records ORDER BY id ASC"
            ).fetchall()

        with output_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=self.COLUMNS,
                extrasaction="ignore",
            )
            writer.writerow({col: self.CSV_HEADERS[col] for col in self.COLUMNS})
            for row in rows:
                writer.writerow(dict(row))
        return len(rows)