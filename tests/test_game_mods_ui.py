"""Windows widget and host-integration checks using a disposable game folder."""

import importlib.util
import json
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    import customtkinter
    import OctoWow_Install_Manager as manager
    import game_mods
    import game_mods_ui

    HAS_UI = sys.platform == "win32"
except ImportError:
    HAS_UI = False


@unittest.skipUnless(HAS_UI, "Requires Windows, tkinter and CustomTkinter")
class GameModsUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        (self.root / "Data").mkdir()
        (self.root / "WoW.exe").write_bytes(b"test fixture")
        payload = self.root / "input.mpq"
        payload.write_bytes(b"MPQ\x1a" + b"fixture")
        self.package = {
            "id": "fixture",
            "name": "Fixture mod",
            "version": "1",
            "source": {"kind": "manual"},
            "files": [
                {
                    "dest": "Data/patch-Q.mpq",
                    "size": payload.stat().st_size,
                    "sha256": game_mods.sha256(payload),
                }
            ],
        }
        self.store = game_mods.Store(self.root)
        self.store.apply(self.package, lambda *args: payload, approve=True)
        (self.root / "octowow_config.json").write_text(json.dumps({"wow_dir": str(self.root)}))
        self.patches = [
            patch.object(manager, "get_persist_path", return_value=str(self.root)),
            patch.object(manager.OctoWowApp, "detect_gpu", return_value="Standard"),
            patch.object(manager.hd_source, "game_running", return_value=False),
            patch.object(manager.OctoWowApp, "trigger_addon_scan"),
            patch.object(manager.OctoWowApp, "check_app_updates"),
            patch.object(game_mods_ui.GameModsPanel, "check"),
        ]
        for mocked in self.patches:
            mocked.start()
        self.app = manager.OctoWowApp()
        self.app.withdraw()
        self.app.update_idletasks()
        self.panel = self.app.mods_panel

    def tearDown(self):
        try:
            for callback in self.app.tk.call("after", "info"):
                self.app.tk.call("after", "cancel", callback)
            self.app.destroy()
        finally:
            for mocked in reversed(self.patches):
                mocked.stop()
            self.temp.cleanup()

    def widgets(self, parent=None):
        for widget in (parent or self.panel.scroll).winfo_children():
            yield widget
            yield from self.widgets(widget)

    def button(self, prefix):
        return next(
            widget
            for widget in self.widgets()
            if isinstance(widget, customtkinter.CTkButton)
            and widget.cget("text").startswith(prefix)
        )

    def wait_for_task(self):
        deadline = time.monotonic() + 10
        while self.panel.busy and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.assertFalse(self.panel.busy, self.panel.status.cget("text"))

    def test_direct_integration_disclosure_switch_and_trash(self):
        self.assertFalse(hasattr(game_mods_ui, "install_hooks"))
        self.assertNotIn("preview", self.app.title().lower())
        self.assertTrue(getattr(self.app.nav_btns["GameMods"], "_mods_icon", None))
        self.assertEqual(self.button("🗑").cget("width"), 32)
        switch = next(
            widget for widget in self.widgets() if isinstance(widget, customtkinter.CTkSwitch)
        )
        self.assertEqual(switch.cget("text"), "Enabled")
        self.button("▸ Files").invoke()
        self.assertTrue(self.panel.expanded_mods)
        file_switch = next(
            widget
            for widget in self.widgets()
            if isinstance(widget, customtkinter.CTkSwitch) and widget.cget("text") == ""
        )
        file_switch.toggle()
        self.wait_for_task()
        self.assertEqual(
            self.store.state()["mods"]["fixture"]["disabled_files"], ["Data/patch-q.mpq"]
        )
        self.button("▾ Files").invoke()
        self.assertFalse(self.panel.expanded_mods)

    def test_invalid_directory_does_not_retain_previous_mod_actions(self):
        self.app.wow_dir.set(str(self.root / "missing-game"))
        self.panel.refresh()
        self.assertIsNone(self.panel.store)
        self.assertEqual(self.panel.scroll.winfo_children(), [])
        self.assertIn("existing WoW installation", self.panel.status.cget("text"))

    def test_removal_requires_confirmation(self):
        with patch.object(manager.messagebox, "askyesno", return_value=False) as confirm:
            self.button("🗑").invoke()
            confirm.assert_called_once()
            self.assertIn("fixture", self.store.state()["mods"])
        with patch.object(manager.messagebox, "askyesno", return_value=True):
            self.button("🗑").invoke()
            self.wait_for_task()
        self.assertNotIn("fixture", self.store.state()["mods"])

    def test_tools_render_inside_existing_window(self):
        self.panel.inventory()
        self.assertEqual(self.panel.report[0], "MPQ file inventory")
        self.button("Back to mods").invoke()
        self.assertIsNone(self.panel.report)
        self.panel.inspect_overlaps()
        self.wait_for_task()
        self.assertEqual(self.panel.report[0], "Overlapping MPQ assets")
        self.assertFalse(
            any(
                isinstance(widget, customtkinter.CTkToplevel)
                for widget in self.app.winfo_children()
            )
        )

    def test_updater_releases_mod_lock_before_notifying_completion(self):
        messages = []

        def record(message):
            if message[0] == "client_dl_done":
                self.assertFalse(self.store.locked())
            messages.append(message)

        with patch.object(self.app, "_sync_client_files", return_value=True), patch.object(
            self.app.msg_queue, "put", side_effect=record
        ):
            self.app._sync_client_thread(str(self.root))
        self.assertIn(("client_dl_done", False), messages)

    def test_updater_stops_when_game_is_running(self):
        with patch.object(manager.hd_source, "game_running", return_value=True), patch.object(
            self.app, "_sync_client_files"
        ) as sync:
            self.app._sync_client_thread(str(self.root))
        sync.assert_not_called()
        messages = []
        while not self.app.msg_queue.empty():
            messages.append(self.app.msg_queue.get_nowait())
        self.assertTrue(any(kind == "client_dl_error" for kind, _ in messages))


if __name__ == "__main__":
    unittest.main()
