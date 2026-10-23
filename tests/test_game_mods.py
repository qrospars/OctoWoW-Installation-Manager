import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))
import game_mods as gm
import hd_source
import mpq_inspect


class ModsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp.name)
        (self.root / "WoW.exe").write_bytes(b"client")
        (self.root / "Data").mkdir()
        self.running = False
        self.store = gm.Store(self.root, lambda: self.running)
        self.payload = self.root / "download.mpq"
        self.payload.write_bytes(b"MPQ\x1a" + b"new content")
        self.p = {
            "id": "test",
            "name": "Example",
            "version": "1",
            "source": {"kind": "manual"},
            "files": [
                {
                    "dest": "Data/patch-Q.mpq",
                    "sha256": gm.sha256(self.payload),
                    "size": self.payload.stat().st_size,
                }
            ],
        }

    def tearDown(self):
        # TemporaryDirectory was created under the explicit test workspace above.
        self.assertEqual(Path(self.temp.name).resolve().parent, Path(__file__).resolve().parent)
        self.temp.cleanup()

    def fetch(self, item, *args):
        return self.payload

    def apply(self, package=None, **kwargs):
        return self.store.apply(package or self.p, self.fetch, approve=True, **kwargs)

    def test_file_switch_preserves_other_files_and_verification(self):
        package = copy.deepcopy(self.p)
        package["files"].append(dict(package["files"][0], dest="Data/patch-R.mpq"))
        self.apply(package)
        self.store.toggle_file("test", "Data/patch-Q.mpq", False)
        self.assertFalse((self.root / "Data/patch-q.mpq").exists())
        self.assertTrue((self.root / "Data/patch-q.mpq.disabled").exists())
        self.assertTrue((self.root / "Data/patch-r.mpq").exists())
        self.assertEqual(self.store.verify("test"), [])
        self.assertEqual(self.store.state()["mods"]["test"]["disabled_files"], ["Data/patch-q.mpq"])

    def test_file_choices_survive_mod_switch_and_content_update(self):
        self.apply()
        self.store.toggle_file("test", "Data/patch-Q.mpq", False)
        self.store.toggle("test", False)
        self.store.toggle("test", True)
        self.assertTrue((self.root / "Data/patch-q.mpq.disabled").exists())
        updated = copy.deepcopy(self.p)
        self.payload.write_bytes(b"MPQ\x1a" + b"updated content")
        updated["version"] = "2"
        updated["files"][0].update(size=self.payload.stat().st_size, sha256=gm.sha256(self.payload))
        self.apply(updated)
        self.assertFalse((self.root / "Data/patch-q.mpq").exists())
        self.assertEqual(
            (self.root / "Data/patch-q.mpq.disabled").read_bytes(), self.payload.read_bytes()
        )
        self.store.toggle_file("test", "Data/patch-Q.mpq", True)
        self.assertTrue((self.root / "Data/patch-q.mpq").exists())

    def test_file_switch_rejects_foreign_file_and_disabled_mod(self):
        self.apply()
        with self.assertRaises(gm.ModError):
            self.store.toggle_file("test", "Data/patch-R.mpq", False)
        self.store.toggle("test", False)
        with self.assertRaises(gm.ModError):
            self.store.toggle_file("test", "Data/patch-Q.mpq", False)

    def test_file_switch_preserves_externally_changed_file(self):
        self.apply()
        target = self.root / "Data/patch-q.mpq"
        target.write_bytes(b"external content")
        with self.assertRaises(gm.ModError):
            self.store.toggle_file("test", "Data/patch-Q.mpq", False)
        self.assertEqual(target.read_bytes(), b"external content")

    def test_file_switch_blocked_while_playing(self):
        self.apply()
        self.running = True
        with self.assertRaises(gm.ModError):
            self.store.toggle_file("test", "Data/patch-Q.mpq", False)
        self.assertTrue((self.root / "Data/patch-q.mpq").exists())

    def test_hd_files_cannot_be_split_by_switch(self):
        self.apply()
        state = self.store.state()
        state["mods"]["test"]["source"] = {"kind": "hd"}
        self.store.save(state)
        with self.assertRaisesRegex(gm.ModError, "compatibility tables"):
            self.store.toggle_file("test", "Data/patch-Q.mpq", False)
        self.assertTrue((self.root / "Data/patch-q.mpq").exists())

    def test_file_switch_can_be_undone(self):
        self.apply()
        self.store.toggle_file("test", "Data/patch-Q.mpq", False)
        self.store.undo()
        self.assertTrue((self.root / "Data/patch-q.mpq").exists())
        self.assertEqual(self.store.state()["mods"]["test"].get("disabled_files", []), [])

    def test_dependency_cannot_use_partially_disabled_mod(self):
        self.apply()
        self.store.toggle_file("test", "Data/patch-Q.mpq", False)
        dependent = copy.deepcopy(self.p)
        dependent.update(id="dependent", requires=["test"])
        dependent["files"][0]["dest"] = "Data/patch-R.mpq"
        with self.assertRaisesRegex(gm.ModError, "required mods"):
            self.apply(dependent)
        self.store.toggle_file("test", "Data/patch-Q.mpq", True)
        self.apply(dependent)
        with self.assertRaisesRegex(gm.ModError, "dependent mods"):
            self.store.toggle_file("test", "Data/patch-Q.mpq", False)

    def test_fresh_install_and_verify(self):
        self.apply()
        self.assertEqual(self.store.verify("test"), [])
        self.assertEqual((self.root / "Data/patch-q.mpq").read_bytes(), self.payload.read_bytes())

    def test_unrelated_files_unchanged(self):
        path = self.root / "Data/patch-X.mpq"
        path.write_bytes(b"other")
        self.apply()
        self.store.remove("test")
        self.assertEqual(path.read_bytes(), b"other")

    def test_protect_numeric_and_core_paths(self):
        for name in (
            "Data/patch-1.mpq",
            "Data/patch.mpq",
            "../WoW.exe",
            "Data/../patch-a.mpq",
            "Data\\patch-a.mpq",
            "Data/patch-A.mpq.disabled",
        ):
            p = copy.deepcopy(self.p)
            p["files"][0]["dest"] = name
            with self.assertRaises(gm.ModError):
                self.apply(p)

    def test_collision_requires_approval(self):
        (self.root / "Data/patch-q.mpq").write_bytes(b"original")
        with self.assertRaises(gm.ModError):
            self.store.apply(self.p, self.fetch)
        self.assertEqual((self.root / "Data/patch-q.mpq").read_bytes(), b"original")

    def test_replaced_original_restored(self):
        target = self.root / "Data/patch-q.mpq"
        target.write_bytes(b"original")
        self.apply()
        self.store.remove("test")
        self.assertEqual(target.read_bytes(), b"original")

    def test_other_mod_cannot_take_owned_filename(self):
        self.apply()
        p = copy.deepcopy(self.p)
        p["id"] = "other"
        with self.assertRaises(gm.ModError):
            self.apply(p)

    def test_game_running_blocks_apply_toggle_remove_recover(self):
        self.apply()
        self.running = True
        for action in (
            lambda: self.apply(),
            lambda: self.store.toggle("test", False),
            lambda: self.store.remove("test"),
            lambda: self.store.recover(),
        ):
            with self.assertRaises(gm.ModError):
                action()
        self.assertTrue((self.root / "Data/patch-q.mpq").exists())

    def test_download_only_while_playing(self):
        self.running = True
        self.apply(download_only=True)
        self.assertFalse((self.root / "Data/patch-q.mpq").exists())

    def test_disable_update_enable(self):
        self.apply()
        self.store.toggle("test", False)
        self.payload.write_bytes(b"MPQ\x1a" + b"updated")
        p = copy.deepcopy(self.p)
        p["version"] = "2"
        p["files"][0].update(sha256=gm.sha256(self.payload), size=self.payload.stat().st_size)
        self.apply(p)
        self.assertFalse((self.root / "Data/patch-q.mpq").exists())
        self.assertEqual(
            (self.root / "Data/patch-q.mpq.disabled").read_bytes(), self.payload.read_bytes()
        )
        self.store.toggle("test", True)
        self.assertTrue((self.root / "Data/patch-q.mpq").exists())

    def test_verify_detects_same_size_external_modification(self):
        self.apply()
        path = self.root / "Data/patch-q.mpq"
        before = path.stat()
        path.write_bytes(b"X" * path.stat().st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(len(self.store.verify("test")), 1)

    def test_remove_keeps_externally_modified_file(self):
        self.apply()
        path = self.root / "Data/patch-q.mpq"
        path.write_bytes(b"changed by game update")
        with self.assertRaises(gm.ModError):
            self.store.remove("test")
        self.assertTrue(path.exists())

    def test_failed_copy_rolls_back(self):
        path = self.root / "Data/patch-q.mpq"
        path.write_bytes(b"original")
        real = gm.shutil.copyfile

        def copy(src, dst, *a, **k):
            if str(dst).endswith(".installing"):
                raise OSError("simulated disk failure")
            return real(src, dst, *a, **k)

        with patch.object(gm.shutil, "copyfile", side_effect=copy):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(self.store.state()["mods"], {})

    def test_recover_persisted_interruption(self):
        home = self.store.home
        tx = home / "transactions/crashed"
        tx.mkdir(parents=True)
        (tx / "0.mpq").write_bytes(b"original")
        (self.root / "Data/patch-q.mpq").write_bytes(b"partial new version")
        gm.atomic_json(
            tx / "journal.json",
            {
                "status": "applying",
                "before": self.store.state(),
                "operations": [{"dest": "Data/patch-q.mpq", "backup": "0.mpq", "existed": True}],
            },
        )
        with self.assertRaises(gm.ModError):
            self.apply()
        self.store.recover()
        self.assertEqual((self.root / "Data/patch-q.mpq").read_bytes(), b"original")

    def test_undo_install(self):
        self.apply()
        self.store.undo()
        self.assertFalse((self.root / "Data/patch-q.mpq").exists())
        self.assertEqual(self.store.state()["mods"], {})

    def test_undo_refuses_external_changes(self):
        self.apply()
        (self.root / "Data/patch-q.mpq").write_bytes(b"external")
        with self.assertRaises(gm.ModError):
            self.store.undo()

    def test_multiple_installations_isolated(self):
        self.apply()
        other = self.root / "second"
        (other / "Data").mkdir(parents=True)
        (other / "WoW.exe").write_bytes(b"client")
        self.assertEqual(gm.Store(other).state()["mods"], {})

    def test_operation_lock_excludes_other_manager(self):
        other = gm.Store(self.root)
        with self.store.lock():
            with self.assertRaises(gm.ModError):
                with other.lock():
                    pass

    def test_missing_dependency(self):
        p = copy.deepcopy(self.p)
        p["requires"] = ["missing"]
        with self.assertRaises(gm.ModError):
            self.apply(p)

    def test_dependency_prevents_removal_and_disabling(self):
        self.apply()
        p = copy.deepcopy(self.p)
        p.update(id="dependent", requires=["test"])
        p["files"][0]["dest"] = "Data/patch-r.mpq"
        self.apply(p)
        for action in (lambda: self.store.remove("test"), lambda: self.store.toggle("test", False)):
            with self.assertRaises(gm.ModError):
                action()

    def test_low_disk_space(self):
        with patch.object(gm.shutil, "disk_usage", return_value=type("Disk", (), {"free": 0})()):
            with self.assertRaises(gm.ModError):
                self.apply()

    def test_cancel_keeps_active_files(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(gm.Cancelled):
            self.apply(cancel=cancel)
        self.assertFalse((self.root / "Data/patch-q.mpq").exists())

    def test_bad_checksum_does_not_install(self):
        p = copy.deepcopy(self.p)
        p["files"][0]["sha256"] = "0" * 64
        with self.assertRaises(gm.ModError):
            self.apply(p)

    def test_inventory_protects_disabled_server_files(self):
        (self.root / "Data/patch-3.mpq.disabled").write_bytes(b"core")
        (self.root / "Data/patch.MPQ.part").write_bytes(b"partial")
        kinds = {r["name"]: r["kind"] for r in self.store.inventory()}
        self.assertEqual(kinds["patch-3.mpq.disabled"], "server")
        self.assertEqual(kinds["patch.MPQ.part"], "incomplete")

    def test_root_mpqs_are_visible(self):
        (self.root / "patch-q.mpq").write_bytes(b"unloaded")
        self.assertEqual(self.store.inventory()[0]["location"], "Game folder")

    def test_corrupt_registry_preserved(self):
        self.store.home.mkdir()
        self.store.state_path.write_text("{broken")
        with self.assertRaises(gm.ModError):
            self.store.state()
        self.assertEqual(self.store.state_path.read_text(), "{broken")

    def test_zip_import_only_mpqs(self):
        archive = self.root / "mod.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("nested/patch-Q.mpq", self.payload.read_bytes())
            z.writestr("nested/not-run.exe", b"executable")
        package, local = gm.import_package([archive], self.root, "Manual ZIP")
        self.assertEqual(len(local), 1)
        self.assertEqual(package["source"]["kind"], "manual")

    def test_zip_traversal_duplicate_and_core_rejected(self):
        for entries in (
            ["../patch-q.mpq"],
            ["one/patch-q.mpq", "two/PATCH-Q.mpq"],
            ["patch-1.mpq"],
        ):
            archive = self.root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as z:
                for name in entries:
                    z.writestr(name, self.payload.read_bytes())
            with self.assertRaises(gm.ModError):
                gm.import_package([archive], self.root, "Unsafe")

    def test_source_urls_and_formats(self):
        self.assertEqual(gm.source_from_url("https://github.com/example/mod")["kind"], "github")
        self.assertEqual(
            gm.source_from_url("https://example.com/game-mod.json")["kind"], "manifest"
        )
        for url in (
            "http://example.com/mod",
            "https://user:password@example.com/mod",
            "https://github.com/example/mod/tree/main",
        ):
            with self.assertRaises(gm.ModError):
                gm.source_from_url(url)

    def test_new_hosts_need_review(self):
        p = copy.deepcopy(self.p)
        p["files"][0]["url"] = "https://example.com/patch-q.mpq"
        self.assertEqual(self.store.preview(p)["new_hosts"], ["example.com"])
        with self.assertRaises(gm.ModError):
            self.store.apply(p, self.fetch)

    def test_source_migration_is_explicit(self):
        self.apply()
        p = copy.deepcopy(self.p)
        p["source"] = {"kind": "manifest", "url": "https://example.com/mod.json"}
        with self.assertRaises(gm.ModError):
            self.store.preview(p)
        self.apply(p, migrate_source=True)
        self.assertEqual(self.store.state()["mods"]["test"]["source"], p["source"])

    def test_changed_file_set_removes_obsolete_owned_files(self):
        self.apply()
        p = copy.deepcopy(self.p)
        p["files"][0]["dest"] = "Data/patch-r.mpq"
        self.apply(p)
        self.assertFalse((self.root / "Data/patch-q.mpq").exists())
        self.assertTrue((self.root / "Data/patch-r.mpq").exists())

    def test_duplicate_manifest_destinations(self):
        p = copy.deepcopy(self.p)
        p["files"].append(copy.deepcopy(p["files"][0]))
        with self.assertRaises(gm.ModError):
            gm.validate_package(p)

    def test_download_resume(self):
        item = dict(self.p["files"][0], url="https://example.com/patch-q.mpq")
        data = self.payload.read_bytes()
        cache = self.store.home / "downloads"
        cache.mkdir(parents=True)
        (cache / (item["sha256"] + ".part")).write_bytes(data[:4])
        response = io.BytesIO(data[4:])
        response.status = 206
        response.headers = {"Content-Range": "bytes 4-%d/%d" % (len(data) - 1, len(data))}
        with patch.object(gm, "open_url", return_value=response) as request:
            path = gm.fetcher(self.store)(item, {"example.com"}, None, lambda *a: None)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(request.call_args.args[2], {"Range": "bytes=4-"})

    def test_server_ignores_range_restarts_cleanly(self):
        item = dict(self.p["files"][0], url="https://example.com/patch-q.mpq")
        cache = self.store.home / "downloads"
        cache.mkdir(parents=True)
        (cache / (item["sha256"] + ".part")).write_bytes(b"old")
        response = io.BytesIO(self.payload.read_bytes())
        response.status = 200
        response.headers = {}
        with patch.object(gm, "open_url", return_value=response):
            path = gm.fetcher(self.store)(item, {"example.com"}, None, lambda *a: None)
        self.assertEqual(path.read_bytes(), self.payload.read_bytes())

    def test_download_checks_content_and_size(self):
        item = dict(self.p["files"][0], url="https://example.com/patch-q.mpq")
        response = io.BytesIO(b"X" * item["size"])
        response.status = 200
        response.headers = {}
        with patch.object(gm, "open_url", return_value=response):
            with self.assertRaises(gm.ModError):
                gm.fetcher(self.store)(item, {"example.com"}, None, lambda *a: None)
        self.assertFalse((self.root / "Data/patch-q.mpq").exists())

    def test_redirect_to_new_host_blocked(self):
        with self.assertRaises(gm.ModError):
            gm.CheckedRedirect({"example.com"}).redirect_request(
                None, None, 302, "", {}, "https://other.example/pack"
            )

    def test_github_adapter_uses_release_asset_digest(self):
        asset = {
            "name": "patch-Q.mpq",
            "digest": "sha256:" + self.p["files"][0]["sha256"],
            "size": self.payload.stat().st_size,
            "browser_download_url": "https://github.com/example/mod/releases/download/1/patch-Q.mpq",
        }
        with patch.object(gm, "json_url", return_value={"tag_name": "1", "assets": [asset]}):
            package = gm.source_package(gm.source_from_url("https://github.com/example/mod"))
        self.assertEqual(package["files"][0]["dest"], "Data/patch-q.mpq")

    def test_github_without_digest_is_manual_only(self):
        asset = {
            "name": "patch-Q.mpq",
            "size": 5,
            "browser_download_url": "https://github.com/example/mod/releases/download/1/patch-Q.mpq",
        }
        with patch.object(gm, "json_url", return_value={"tag_name": "1", "assets": [asset]}):
            with self.assertRaises(gm.ModError):
                gm.source_package(gm.source_from_url("https://github.com/example/mod"))

    def test_manifest_adapter_and_schema_changes(self):
        p = dict(copy.deepcopy(self.p), schema=1)
        source = {"kind": "manifest", "url": "https://example.com/game-mod.json"}
        with patch.object(gm, "json_url", return_value=p):
            self.assertEqual(gm.source_package(source)["id"], "test")
        p["schema"] = 99
        with patch.object(gm, "json_url", return_value=p):
            with self.assertRaises(gm.ModError):
                gm.source_package(source)

    def test_migrated_source_keeps_mod_identity_on_future_checks(self):
        source = {
            "kind": "manifest",
            "url": "https://example.com/mod.json",
            "target_id": "existing-id",
        }
        p = dict(copy.deepcopy(self.p), schema=1)
        with patch.object(gm, "json_url", return_value=p):
            self.assertEqual(gm.source_package(source)["id"], "existing-id")

    def test_network_failure_preserves_installed_files(self):
        self.apply()
        before = (self.root / "Data/patch-q.mpq").read_bytes()
        with patch.object(gm, "json_url", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                gm.source_package({"kind": "manifest", "url": "https://example.com/mod.json"})
        self.assertEqual((self.root / "Data/patch-q.mpq").read_bytes(), before)

    def test_hd_source_change_fails_without_changing_files(self):
        manifest = json.loads((Path(__file__).parent / "hd-manifest-v3.json").read_text())
        self.assertEqual(len(hd_source.validate_manifest(manifest)["files"]), 15)
        manifest["components"][1]["files"][0]["url"] = "https://new.example.com/patch-o.mpq"
        with self.assertRaises(gm.ModError):
            hd_source.validate_manifest(manifest)

    def test_mpq_hash_reference_and_truncated_archive(self):
        self.assertEqual(mpq_inspect.name_hash("(hash table)", 3), 0xC3AF3770)
        with self.assertRaises(ValueError):
            mpq_inspect.fingerprints(self.payload)

    def test_two_independent_sources_can_install(self):
        first = copy.deepcopy(self.p)
        first["source"] = {"kind": "manifest", "url": "https://one.example/mod.json"}
        second = copy.deepcopy(first)
        second["id"] = "second"
        second["source"] = {"kind": "manifest", "url": "https://two.example/mod.json"}
        second["files"][0]["dest"] = "Data/patch-r.mpq"
        self.apply(first)
        self.apply(second)
        self.assertEqual(len(self.store.state()["mods"]), 2)

    def test_same_version_new_hash_counts_as_update(self):
        changed = copy.deepcopy(self.p)
        changed["files"][0]["sha256"] = "a" * 64
        self.assertTrue(gm.package_changed(self.p, changed))

    def test_disabled_original_restored_disabled(self):
        disabled = self.root / "Data/patch-q.mpq.disabled"
        disabled.write_bytes(b"original disabled mod")
        self.apply()
        self.store.remove("test")
        self.assertFalse((self.root / "Data/patch-q.mpq").exists())
        self.assertEqual(disabled.read_bytes(), b"original disabled mod")

    def test_enabled_original_restored_after_disabling_managed_mod(self):
        enabled = self.root / "Data/patch-q.mpq"
        enabled.write_bytes(b"original")
        self.apply()
        self.store.toggle("test", False)
        self.store.remove("test")
        self.assertEqual(enabled.read_bytes(), b"original")
        self.assertFalse((self.root / "Data/patch-q.mpq.disabled").exists())

    def test_missing_client_plugin_blocks_install(self):
        p = copy.deepcopy(self.p)
        p["requires_dlls"] = ["Example.dll"]
        with self.assertRaises(gm.ModError):
            self.apply(p)

    def test_client_plugin_must_be_enabled_in_loader(self):
        p = copy.deepcopy(self.p)
        p["requires_dlls"] = ["Example.dll"]
        (self.root / "Example.dll").write_bytes(b"plugin")
        with self.assertRaises(gm.ModError):
            self.apply(p)
        (self.root / "dlls.txt").write_text("Example.dll\n")
        self.apply(p)

    def test_interrupted_recovery_preserves_external_change(self):
        tx = self.store.home / "transactions/crashed"
        tx.mkdir(parents=True)
        (tx / "0.mpq").write_bytes(b"original")
        current = self.root / "Data/patch-q.mpq"
        current.write_bytes(b"external edit")
        gm.atomic_json(
            tx / "journal.json",
            {
                "format": 2,
                "status": "applying",
                "before": self.store.state(),
                "operations": [
                    {
                        "dest": "Data/patch-q.mpq",
                        "backup": "0.mpq",
                        "existed": True,
                        "before_sha": gm.sha256(tx / "0.mpq"),
                        "new_sha": gm.sha256(self.payload),
                    }
                ],
            },
        )
        with self.assertRaises(gm.ModError):
            self.store.recover()
        self.assertEqual(current.read_bytes(), b"external edit")

    def test_cancel_after_last_download_does_not_apply(self):
        cancel = threading.Event()

        def fetch(item, *args):
            cancel.set()
            return self.payload

        with self.assertRaises(gm.Cancelled):
            self.store.apply(self.p, fetch, approve=True, cancel=cancel)
        self.assertFalse((self.root / "Data/patch-q.mpq").exists())


if __name__ == "__main__":
    unittest.main()
