"""Tests for citracer.metadata_cache — SQLite-backed KV store."""
import threading

from citracer.metadata_cache import MetadataCache


class TestMetadataCache:
    def test_miss_returns_false_none(self, tmp_path):
        cache = MetadataCache(tmp_path / "cache.sqlite")
        hit, val = cache.get("s2", "nonexistent")
        assert hit is False
        assert val is None

    def test_roundtrip_dict(self, tmp_path):
        cache = MetadataCache(tmp_path / "cache.sqlite")
        payload = {"arxiv_id": "2211.14730", "title": "PatchTST", "year": 2022}
        cache.set("s2", "key1", payload)
        hit, val = cache.get("s2", "key1")
        assert hit is True
        assert val == payload

    def test_negative_cache(self, tmp_path):
        cache = MetadataCache(tmp_path / "cache.sqlite")
        cache.set("arxsearch", "missing_paper", None)
        hit, val = cache.get("arxsearch", "missing_paper")
        assert hit is True
        assert val is None

    def test_sources_are_isolated(self, tmp_path):
        cache = MetadataCache(tmp_path / "cache.sqlite")
        cache.set("s2", "shared_key", {"from": "s2"})
        cache.set("arxsearch", "shared_key", {"from": "arxiv"})
        assert cache.get("s2", "shared_key") == (True, {"from": "s2"})
        assert cache.get("arxsearch", "shared_key") == (True, {"from": "arxiv"})

    def test_overwrite(self, tmp_path):
        cache = MetadataCache(tmp_path / "cache.sqlite")
        cache.set("s2", "key", {"v": 1})
        cache.set("s2", "key", {"v": 2})
        _, val = cache.get("s2", "key")
        assert val == {"v": 2}

    def test_unicode_values(self, tmp_path):
        cache = MetadataCache(tmp_path / "cache.sqlite")
        payload = {"title": "日本語 paper with émojis 🎉"}
        cache.set("s2", "unicode", payload)
        _, val = cache.get("s2", "unicode")
        assert val == payload

    def test_persists_across_instances(self, tmp_path):
        db = tmp_path / "cache.sqlite"
        c1 = MetadataCache(db)
        c1.set("s2", "persistent", {"foo": "bar"})
        c1.close()
        c2 = MetadataCache(db)
        hit, val = c2.get("s2", "persistent")
        assert hit is True
        assert val == {"foo": "bar"}

    def test_thread_safe_writes(self, tmp_path):
        cache = MetadataCache(tmp_path / "cache.sqlite")

        def writer(i: int):
            for j in range(20):
                cache.set("s2", f"k{i}_{j}", {"i": i, "j": j})

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for i in range(4):
            for j in range(20):
                hit, val = cache.get("s2", f"k{i}_{j}")
                assert hit is True
                assert val == {"i": i, "j": j}
