"""MT-P2 R5 备份机制测试（docs/17 R5 / docs/20-C3）。

验证 _backup_db_before_migration：
1. 备份文件 integrity_check 通过；
2. WAL 中未 checkpoint 的事务不丢失（VACUUM INTO 经 WAL 读取）；
3. 轮转只保留最新 3 份（含 .sanitized 副本联动删除）；
4. 脱敏副本不含密钥明文、schema 完整、原备份保留明文；
5. 备份失败（自检不过 / VACUUM 报错）即抛错阻断迁移。
"""

import glob
import os
import sqlite3

import pytest

from src.web.database import _backup_db_before_migration


def _make_wal_db(db_path: str) -> sqlite3.Connection:
    """建一个 WAL 模式、含敏感数据的最小库，返回保持打开的连接。

    连接保持打开且不 checkpoint，使最近事务驻留 -wal 文件，
    用于验证备份经 WAL 读取不丢数据。
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE ai_services ("
        "id INTEGER PRIMARY KEY, name TEXT, base_url TEXT, api_key TEXT)"
    )
    conn.execute(
        "CREATE TABLE notify_channels ("
        "id INTEGER PRIMARY KEY, name TEXT, type TEXT, config TEXT)"
    )
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO ai_services VALUES (1, 'openai', 'https://api', 'sk-SECRET-AI-KEY')"
    )
    conn.execute(
        "INSERT INTO notify_channels VALUES "
        "(1, 'tg', 'telegram', '{\"bot_token\": \"SECRET-BOT-TOKEN\"}')"
    )
    conn.executemany(
        "INSERT INTO app_settings VALUES (?, ?)",
        [
            ("jwt_secret", "SECRET-JWT"),
            ("auth_username", "admin"),
            ("auth_password_hash", "SECRET-PW-HASH"),
            ("http_proxy", "http://proxy:8080"),
            ("ui_avatar", "avatar.png"),
        ],
    )
    return conn


def _integrity_ok(db_path: str) -> bool:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_backup_integrity_check_passes(tmp_path):
    db_path = str(tmp_path / "panwatch.db")
    conn = _make_wal_db(db_path)
    try:
        backup = _backup_db_before_migration(db_path)
    finally:
        conn.close()
    assert backup and os.path.exists(backup)
    assert _integrity_ok(backup)


def test_backup_preserves_uncommitted_checkpoint_wal_data(tmp_path):
    """WAL 未 checkpoint 的事务必须进入备份（旧 copy2 实现会丢）。"""
    db_path = str(tmp_path / "panwatch.db")
    conn = _make_wal_db(db_path)
    # 写入后不 checkpoint，数据驻留 -wal；连接保持打开防止关闭时自动回收
    conn.execute(
        "INSERT INTO app_settings VALUES ('latest_wal_row', 'wal-value-123')"
    )
    assert os.path.exists(db_path + "-wal")

    backup = _backup_db_before_migration(db_path)
    conn.close()

    bak = sqlite3.connect(backup, timeout=30)
    try:
        value = bak.execute(
            "SELECT value FROM app_settings WHERE key = 'latest_wal_row'"
        ).fetchone()
        api_key = bak.execute(
            "SELECT api_key FROM ai_services WHERE id = 1"
        ).fetchone()
    finally:
        bak.close()
    assert value and value[0] == "wal-value-123"
    # 原备份（卷内）保留完整明文，可正常恢复
    assert api_key and api_key[0] == "sk-SECRET-AI-KEY"


def test_rotation_keeps_latest_three(tmp_path):
    db_path = str(tmp_path / "panwatch.db")
    conn = _make_wal_db(db_path)
    try:
        # 预造 3 份历史备份（含 .sanitized），再触发一次新备份 → 共 4 份 → 淘汰最旧
        for ts in ("20200101_000001", "20200101_000002", "20200101_000003"):
            old = f"{db_path}.bak.{ts}"
            with open(old, "wb") as f:
                f.write(b"old")
            with open(old + ".sanitized", "wb") as f:
                f.write(b"old")
        backup = _backup_db_before_migration(db_path, keep=3)
    finally:
        conn.close()

    baks = sorted(
        p for p in glob.glob(f"{db_path}.bak.*") if not p.endswith(".sanitized")
    )
    sanitized = glob.glob(f"{db_path}.bak.*.sanitized")
    assert len(baks) == 3
    assert baks[-1] == backup
    # 最旧的 20200101_000001 及其 .sanitized 被淘汰
    assert not any("20200101_000001" in p for p in baks + sanitized)
    # 留下的历史备份仍带各自 .sanitized，新备份也有
    assert os.path.exists(backup + ".sanitized")
    assert len(sanitized) == 3


def test_sanitized_copy_has_no_secrets_but_intact_schema(tmp_path):
    db_path = str(tmp_path / "panwatch.db")
    conn = _make_wal_db(db_path)
    try:
        backup = _backup_db_before_migration(db_path)
    finally:
        conn.close()
    sanitized = backup + ".sanitized"
    assert os.path.exists(sanitized)
    assert _integrity_ok(sanitized)

    with open(sanitized, "rb") as f:
        raw = f.read()
    for secret in (b"sk-SECRET-AI-KEY", b"SECRET-BOT-TOKEN", b"SECRET-JWT",
                   b"SECRET-PW-HASH", b"admin"):
        assert secret not in raw

    conn2 = sqlite3.connect(sanitized, timeout=30)
    try:
        # schema 完整、行数不变（置空而非删行），可直接恢复
        assert conn2.execute("SELECT COUNT(*) FROM ai_services").fetchone()[0] == 1
        assert conn2.execute(
            "SELECT api_key FROM ai_services WHERE id = 1"
        ).fetchone()[0] == ""
        assert conn2.execute(
            "SELECT config FROM notify_channels WHERE id = 1"
        ).fetchone()[0] == ""
        settings = dict(conn2.execute("SELECT key, value FROM app_settings"))
    finally:
        conn2.close()
    assert settings["jwt_secret"] == ""
    assert settings["auth_username"] == ""
    assert settings["auth_password_hash"] == ""
    # 非敏感键保留
    assert settings["http_proxy"] == "http://proxy:8080"
    assert settings["ui_avatar"] == "avatar.png"


def test_backup_failure_blocks_migration(tmp_path):
    """源文件非合法 sqlite → VACUUM INTO 失败 → 抛错阻断，不残留备份文件。"""
    db_path = str(tmp_path / "panwatch.db")
    with open(db_path, "wb") as f:
        f.write(b"not-a-sqlite-database" * 100)

    with pytest.raises(RuntimeError, match="备份失败"):
        _backup_db_before_migration(db_path)
    assert glob.glob(f"{db_path}.bak.*") == []


def test_integrity_check_failure_blocks_migration(tmp_path, monkeypatch):
    """自检不过 → 删备份 + 抛错（模拟备份文件损坏场景）。"""
    db_path = str(tmp_path / "panwatch.db")
    conn = _make_wal_db(db_path)
    monkeypatch.setattr(
        "src.web.database._verify_backup_integrity",
        lambda p: (_ for _ in ()).throw(
            RuntimeError(f"备份自检失败，已阻断迁移: {p}")
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="自检失败"):
            _backup_db_before_migration(db_path)
    finally:
        conn.close()


def test_missing_or_empty_db_skips(tmp_path):
    missing = str(tmp_path / "nope.db")
    assert _backup_db_before_migration(missing) == ""
    empty = str(tmp_path / "empty.db")
    open(empty, "wb").close()
    assert _backup_db_before_migration(empty) == ""
