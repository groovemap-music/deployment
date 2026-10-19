"""Tests for scripts/migrate-redis-to-valkey.py using fakeredis only (no live server)."""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any, cast

import fakeredis
import pytest
import valkey


ROOT = Path(__file__).resolve().parents[2]

MOD: dict[str, Any] = runpy.run_path(str(ROOT / "scripts/migrate-redis-to-valkey.py"), run_name="migrate")
PREFIXES = MOD["PREFIXES"]


def _pttl(client: fakeredis.FakeValkey, key: str) -> int:
    return cast("int", client.pttl(key))


def _pair() -> tuple[fakeredis.FakeValkey, fakeredis.FakeValkey]:
    return (
        fakeredis.FakeValkey(server=fakeredis.FakeServer()),
        fakeredis.FakeValkey(server=fakeredis.FakeServer()),
    )


def test_prefixes_are_exactly_the_design() -> None:
    assert PREFIXES == (
        "revoked:jti:",
        "password_changed:",
        "2fa_challenge:",
        "reset:",
        "discogs:oauth:state:",
        "sync:cooldown:",
    )


def test_ttl_preserved_and_revoked_token_stays_revoked() -> None:
    src, dst = _pair()
    src.set("revoked:jti:abc", "1", px=120_000)
    results = MOD["copy_prefixes"](src, dst)
    assert dst.get("revoked:jti:abc") == b"1"
    assert 0 < _pttl(dst, "revoked:jti:abc") <= 120_000
    assert _pttl(dst, "revoked:jti:abc") > 100_000
    assert results["revoked:jti:"].copied == 1


def test_key_without_ttl_copied_without_expiry() -> None:
    src, dst = _pair()
    src.set("password_changed:42", "1700000000")
    results = MOD["copy_prefixes"](src, dst)
    assert dst.get("password_changed:42") == b"1700000000"
    assert _pttl(dst, "password_changed:42") == -1
    assert results["password_changed:"].persistent == 1


def test_cache_and_unlisted_keys_are_never_copied() -> None:
    src, dst = _pair()
    src.set("cache:artist:1", "x")
    src.set("revoked:other", "x")
    src.set("xreset:1", "x")
    src.set("reset:1", "tok", ex=60)
    src.hset("sync:cooldown:h", "a", "b")  # wrong type: counted as a failure, not silently dropped
    results = MOD["copy_prefixes"](src, dst)
    assert sorted(cast("list[bytes]", dst.keys("*"))) == [b"reset:1"]
    assert results["sync:cooldown:"].failed == 1


def test_all_prefixes_copied() -> None:
    src, dst = _pair()
    for p in PREFIXES:
        src.set(p + "k", "v", ex=300)
    results = MOD["copy_prefixes"](src, dst)
    assert {p: r.copied for p, r in results.items()} == dict.fromkeys(PREFIXES, 1)
    assert dst.dbsize() == len(PREFIXES)


def test_key_expired_between_scan_and_get_is_skipped() -> None:
    src, dst = _pair()
    src.set("reset:gone", "v", ex=60)
    real_get = src.get

    def get_after_expiry(name: Any) -> Any:
        src.delete(name)
        return real_get(name)

    src.get = get_after_expiry  # type: ignore[method-assign]
    results = MOD["copy_prefixes"](src, dst)
    assert results["reset:"].skipped_expired == 1
    assert results["reset:"].failed == 0
    assert dst.dbsize() == 0


def test_dry_run_writes_nothing_and_reports_counts() -> None:
    src, dst = _pair()
    src.set("revoked:jti:a", "1", ex=60)
    src.set("revoked:jti:b", "1", ex=60)
    results = MOD["copy_prefixes"](src, dst, dry_run=True)
    assert dst.dbsize() == 0
    assert results["revoked:jti:"].copied == 2
    report = MOD["format_report"](results, dry_run=True)
    assert "DRY RUN" in report
    assert "revoked:jti:" in report
    assert "would copy=2" in report


def test_failed_copy_exits_nonzero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    src, dst = _pair()
    src.set("reset:1", "v", ex=60)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise valkey.ResponseError("OOM command not allowed")

    dst.set = boom  # type: ignore[method-assign]
    monkeypatch.setitem(MOD["main"].__globals__, "connect", lambda url, _pf: src if "src" in url else dst)
    assert MOD["main"](["--source-url", "redis://src", "--target-url", "redis://dst"]) == 1
    assert "FAILED reset:1" in capsys.readouterr().err


def test_main_success_and_dry_run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    src, dst = _pair()
    src.set("revoked:jti:z", "1", ex=60)
    monkeypatch.setitem(MOD["main"].__globals__, "connect", lambda url, _pf: src if "src" in url else dst)
    argv = ["--source-url", "redis://src", "--target-url", "redis://dst"]
    assert MOD["main"]([*argv, "--dry-run"]) == 0
    assert dst.dbsize() == 0
    assert MOD["main"](argv) == 0
    assert dst.get("revoked:jti:z") == b"1"
    assert "copied=1" in capsys.readouterr().out


def test_identical_urls_rejected() -> None:
    assert MOD["main"](["--source-url", "redis://x", "--target-url", "redis://x"]) == 2


def test_password_files_are_read_and_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pw = tmp_path / "pw"
    pw.write_text("s3cret\n")
    seen: dict[str, Any] = {}

    def fake_from_url(url: str, **kwargs: Any) -> str:
        seen["url"], seen["kwargs"] = url, kwargs
        return "client"

    monkeypatch.setattr(valkey.Valkey, "from_url", staticmethod(fake_from_url))
    assert MOD["connect"]("redis://h:6379/0", str(pw)) == "client"
    assert seen["kwargs"] == {"decode_responses": False, "password": "s3cret"}
    MOD["connect"]("redis://h:6379/0", None)
    assert "password" not in seen["kwargs"]


def test_empty_password_file_rejected(tmp_path: Path) -> None:
    pw = tmp_path / "pw"
    pw.write_text("\n")
    with pytest.raises(ValueError, match="empty"):
        MOD["read_password"](str(pw))


def test_auth_failure_exits_nonzero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class Denied:
        def ping(self) -> None:
            raise valkey.AuthenticationError("WRONGPASS")

    monkeypatch.setitem(MOD["main"].__globals__, "connect", lambda _u, _p: Denied())
    assert MOD["main"](["--source-url", "redis://a", "--target-url", "redis://b"]) == 1
    assert "WRONGPASS" in capsys.readouterr().err
