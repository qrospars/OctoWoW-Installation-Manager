"""Addon-style Game Mods cards. UI work stays on Tk's main thread."""

import json
import queue
import threading
import time
import webbrowser
from pathlib import Path

import game_mods
import hd_source
import mpq_inspect

# --- GAME MOD SOURCES ---
HD_SOURCE = {"kind": "hd", "url": "https://github.com/fmustafayaman/OctoWoW-HD-Switch"}


def source_key(source):
    return json.dumps(source, sort_keys=True)


# --- THEME HELPERS ---
class GameModsTheme:
    """Use the existing manager palette instead of CustomTkinter's blue defaults."""

    def __init__(self, namespace):
        self.namespace = namespace
        self.ctk = namespace["ctk"]

    def create_button(self, *args, appearance="default", **kwargs):
        if appearance == "destructive":
            fg_color = self.namespace["ERROR_COLOR"]
            hover_color = "#DC2626"
        elif appearance == "primary":
            fg_color = self.namespace["ACCENT_COLOR"]
            hover_color = self.namespace["ACCENT_HOVER"]
        else:
            fg_color = self.namespace["CARD_COLOR"]
            hover_color = "#2A2E3F"
        defaults = dict(
            font=("Segoe UI", 12, "bold"),
            text_color=self.namespace["TEXT_MAIN"],
            fg_color=fg_color,
            hover_color=hover_color,
        )
        return self.ctk.CTkButton(*args, **(defaults | kwargs))

    def create_label(self, *args, **kwargs):
        return self.ctk.CTkLabel(
            *args, **(dict(font=("Segoe UI", 12), text_color=self.namespace["TEXT_MAIN"]) | kwargs)
        )

    def create_entry(self, *args, **kwargs):
        return self.ctk.CTkEntry(
            *args,
            **(
                dict(
                    fg_color=self.namespace["BG_COLOR"],
                    border_color=self.namespace["CARD_COLOR"],
                    text_color=self.namespace["TEXT_MAIN"],
                    placeholder_text_color=self.namespace["TEXT_MUTED"],
                    font=("Segoe UI", 12),
                )
                | kwargs
            )
        )

    def create_textbox(self, *args, **kwargs):
        return self.ctk.CTkTextbox(
            *args,
            **(
                dict(
                    fg_color=self.namespace["BG_COLOR"],
                    border_color=self.namespace["CARD_COLOR"],
                    text_color=self.namespace["TEXT_MAIN"],
                    font=("Segoe UI", 12),
                )
                | kwargs
            )
        )

    def create_progress_bar(self, *args, **kwargs):
        return self.ctk.CTkProgressBar(
            *args,
            **(
                dict(
                    fg_color=self.namespace["CARD_COLOR"],
                    progress_color=self.namespace["SUCCESS_COLOR"],
                    height=4,
                    corner_radius=0,
                )
                | kwargs
            )
        )


# --- GAME MODS INTERFACE ---
class GameModsPanel:
    def __init__(self, app, namespace):
        self.app = app
        self.namespace = namespace
        self.ctk = namespace["ctk"]
        self.theme = GameModsTheme(namespace)
        self.messagebox = namespace["messagebox"]
        self.dialog = namespace["filedialog"]
        self.events = queue.Queue()
        self.busy = False
        self.cancel = threading.Event()
        self.latest = {}
        self.errors = {}
        self.expanded_mods = set()
        self.report = None
        self.frame = self.ctk.CTkFrame(app.main_container, fg_color=namespace["BG_COLOR"])
        app.frames["GameMods"] = self.frame
        top = self.ctk.CTkFrame(self.frame, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(10, 5))
        self.theme.create_label(top, text="Game Mods", font=("Segoe UI", 24, "bold")).pack(
            side="left"
        )
        self.controls = []
        self.add_menu_button = self.menu_button(
            top,
            "Add mod",
            [
                ("From a source URL…", self.add_source),
                ("From an MPQ or ZIP file…", self.import_dialog),
            ],
            appearance="primary",
            width=120,
        )
        self.add_menu_button.pack(side="right", padx=4)
        self.controls.append(self.add_menu_button)
        check_button = self.theme.create_button(
            top,
            text="Check updates",
            width=120,
            command=self.check,
        )
        check_button.pack(side="right", padx=4)
        self.controls.append(check_button)
        self.theme.create_label(
            self.frame,
            text="Add and update your game mods. Changes are reviewed before installation.",
            text_color=namespace["TEXT_MUTED"],
        ).pack(anchor="w", padx=10, pady=(0, 15))
        self.search = self.ctk.StringVar()
        search_frame = self.ctk.CTkFrame(
            self.frame, fg_color=namespace["SURFACE_COLOR"], corner_radius=8
        )
        search_frame.pack(fill="x", padx=10)
        self.theme.create_entry(
            search_frame, textvariable=self.search, placeholder_text="Search mods, sources or files"
        ).pack(fill="x", padx=15, pady=10)
        self.update_all_button = self.theme.create_button(
            self.frame, text="Update all", command=self.update_all, appearance="primary"
        )
        self.controls.append(self.update_all_button)
        self.search.trace_add("write", lambda *a: self.render())
        self.status = self.theme.create_label(
            self.frame,
            text="Reading installed mods...",
            wraplength=720,
            justify="left",
            anchor="w",
            text_color=namespace["TEXT_MUTED"],
        )
        self.status.pack(fill="x", padx=10, pady=4)
        row = self.ctk.CTkFrame(self.frame, fg_color="transparent")
        self.progress_row = row
        self.progress = self.theme.create_progress_bar(row)
        self.progress.set(0)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.pause_button = self.theme.create_button(
            row, text="Pause download", width=110, command=self.cancel.set, state="disabled"
        )
        self.pause_button.pack(side="right")
        self.scroll = namespace["SmoothScrollableFrame"](self.frame, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True, padx=5, pady=6)
        footer = self.ctk.CTkFrame(self.frame, fg_color="transparent")
        footer.pack(fill="x", padx=10, pady=8)
        tools_button = self.menu_button(
            footer,
            "Tools",
            [
                ("Installed files…", self.inventory),
                ("Check conflicts between mods…", self.inspect_overlaps),
                None,
                ("Undo last change…", self.undo),
                ("Recover interrupted installation…", self.recover),
                ("Restore mod records…", self.restore_records),
            ],
            width=80,
        )
        tools_button.pack(side="right")
        self.controls.append(tools_button)
        self.recovery_button = self.theme.create_button(
            footer,
            text="Recover interrupted installation",
            command=self.recover,
            appearance="primary",
        )
        self.controls.append(self.recovery_button)
        self.store = None
        self.root = None
        self.app.after(150, self.poll)
        self.refresh()
        app.wow_dir.trace_add("write", lambda *a: app.after(100, self.refresh))

    def menu_button(self, parent, text, actions, **kwargs):
        """Keep secondary actions in a keyboard-accessible, themed menu."""
        button = self.theme.create_button(parent, text=text, **kwargs)

        def show_menu():
            if self.busy:
                return
            previous_menu = getattr(button, "_mods_menu", None)
            if previous_menu is not None:
                previous_menu.destroy()
            previous_menu = getattr(button, "_mods_menu", None)
            if previous_menu is not None:
                previous_menu.destroy()
            menu = self.namespace["tk"].Menu(
                button,
                tearoff=False,
                bg=self.namespace["SURFACE_COLOR"],
                fg=self.namespace["TEXT_MAIN"],
                activebackground=self.namespace["ACCENT_COLOR"],
                activeforeground=self.namespace["TEXT_MAIN"],
                font=("Segoe UI", 12),
                borderwidth=0,
            )
            button._mods_menu = menu
            button._mods_menu = menu
            for action in actions:
                if action is None:
                    menu.add_separator()
                else:
                    label, command = action
                    menu.add_command(label=label, command=command)
            try:
                menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
            finally:
                menu.grab_release()

        button.configure(command=show_menu)
        return button

    def sidebar(self, suffix=""):
        # Match the sidebar's compact white outline icons without relying on emoji fonts.
        button = self.app.nav_btns.get("GameMods")
        if button:
            button.configure(text="        Game Mods" + suffix)
            if not getattr(button, "_mods_icon", None):
                canvas = self.namespace["tk"].Canvas(
                    button,
                    width=16,
                    height=16,
                    bg=self.namespace["SURFACE_COLOR"],
                    highlightthickness=0,
                )
                canvas.create_polygon(
                    2,
                    5,
                    8,
                    2,
                    14,
                    5,
                    8,
                    8,
                    fill="",
                    outline="#F8FAFC",
                    width=1.5,
                    joinstyle="round",
                )
                canvas.create_line(
                    2,
                    8,
                    8,
                    11,
                    14,
                    8,
                    fill="#F8FAFC",
                    width=1.5,
                    capstyle="round",
                    joinstyle="round",
                )
                canvas.create_line(
                    2,
                    11,
                    8,
                    14,
                    14,
                    11,
                    fill="#F8FAFC",
                    width=1.5,
                    capstyle="round",
                    joinstyle="round",
                )
                canvas.place(x=16, rely=0.5, anchor="w")
                canvas.bind("<Button-1>", lambda e: self.app.show_tab("GameMods"))

                def background(hover=False):
                    selected = self.app.current_tab == "GameMods"
                    color = (
                        self.namespace["ACCENT_HOVER"]
                        if selected and hover
                        else (
                            self.namespace["ACCENT_COLOR"]
                            if selected
                            else (
                                self.namespace["CARD_COLOR"]
                                if hover
                                else self.namespace["SURFACE_COLOR"]
                            )
                        )
                    )
                    canvas.configure(bg=color)

                button.bind("<Enter>", lambda e: background(True), add=True)
                button.bind("<Leave>", lambda e: background(False), add=True)
                canvas.bind("<Enter>", lambda e: (button._on_enter(), background(True)))
                canvas.bind("<Leave>", lambda e: (button._on_leave(), background(False)))
                button._mods_icon = canvas
                background()

    def set_busy(self, value):
        self.busy = value
        if value:
            self.progress.set(0)
            self.progress_row.pack(fill="x", padx=10, pady=(0, 5), before=self.scroll._parent_frame)
        else:
            self.progress_row.pack_forget()
        self.pause_button.configure(state="normal" if value else "disabled")
        for button in self.controls:
            button.configure(state="disabled" if value else "normal")
        self.render()

    def task(self, label, operation, done=None):
        if self.busy:
            return
        root = self.root
        self.cancel.clear()
        self.set_busy(True)
        self.status.configure(text=label)

        def progress(text, fraction=None):
            self.events.put(("progress", root, (text, fraction)))

        def worker():
            try:
                result = operation(progress)
                self.events.put(("done", root, (result, done, label)))
            except Exception as exc:
                self.events.put(("error", root, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def refresh(self):
        if self.busy:
            return
        try:
            root = Path(self.app.wow_dir.get().strip()).resolve()
            if root != self.root:
                self.store = None
                for widget in self.scroll.winfo_children():
                    widget.destroy()
                self.report = None
                self.latest.clear()
                self.errors.clear()
            self.root = root
            self.store = game_mods.Store(root, lambda: hd_source.game_running(root))
            state = self.store.state()
            for source in self.sources():
                key = source_key(source)
                cache = (
                    self.store.home
                    / "source-cache"
                    / (game_mods.hashlib.sha256(key.encode()).hexdigest() + ".json")
                )
                if key not in self.latest and cache.exists():
                    try:
                        self.latest[key] = game_mods.validate_package(
                            json.loads(cache.read_text("utf-8"))["package"]
                        )
                        self.errors[key] = (
                            "Showing previously checked metadata; waiting for an online update check."
                        )
                    except (ValueError, OSError, game_mods.ModError, KeyError):
                        pass
            self.render()
            self.sidebar()
            if any(
                json.loads(p.read_text())["status"] == "applying"
                for p in self.store.home.glob("transactions/*/journal.json")
            ):
                self.status.configure(
                    text="An operation was interrupted. Close WoW and choose Recover interrupted work."
                )
            self.app.after(300, self.check)
        except Exception as exc:
            self.status.configure(text=str(exc))

    def sources(self):
        state = self.store.state()
        sources = (
            (
                [HD_SOURCE]
                if not any(
                    mid == "hd-standard" and m["source"] != HD_SOURCE
                    for mid, m in state["mods"].items()
                )
                else []
            )
            + state.get("sources", [])
            + [m["source"] for m in state["mods"].values() if m["source"]["kind"] != "manual"]
        )
        return list({source_key(s): s for s in sources}.values())

    def check(self):
        if self.busy or not self.store:
            return
        try:
            sources = self.sources()
        except Exception as exc:
            self.status.configure(text=str(exc))
            return
        store = self.store

        def work(progress):
            results = []
            for source in sources:
                if self.cancel.is_set():
                    break
                try:
                    package = game_mods.source_package(source, hd_source)
                    results.append((source_key(source), package, None))
                    cache = (
                        store.home
                        / "source-cache"
                        / (
                            game_mods.hashlib.sha256(source_key(source).encode()).hexdigest()
                            + ".json"
                        )
                    )
                    game_mods.atomic_json(cache, {"checked": time.time(), "package": package})
                except Exception as exc:
                    results.append(
                        (source_key(source), None, "Update check unavailable: " + str(exc))
                    )
            return results

        def done(results):
            for key, package, error in results:
                if package:
                    self.latest[key] = package
                    self.errors.pop(key, None)
                else:
                    self.errors[key] = error
            self.status.configure(text="Update check complete.")
            self.render()

        self.task("Checking tracked sources...", work, done)

    def render(self):
        if not self.store:
            return
        for widget in self.scroll.winfo_children():
            widget.destroy()
        if self.report is not None:
            self.render_report()
            return
        try:
            state = self.store.state()
        except Exception as exc:
            self.status.configure(text=str(exc))
            return
        installed_sources = {source_key(m["source"]) for m in state["mods"].values()}
        entries = [(m, self.latest.get(source_key(m["source"]))) for m in state["mods"].values()]
        entries += [(None, p) for key, p in self.latest.items() if key not in installed_sources]
        query = self.search.get().strip().lower()
        updates = sum(
            bool(
                installed
                and latest
                and source_key(installed["source"]) not in self.errors
                and game_mods.package_changed(installed, latest)
            )
            for installed, latest in entries
        )
        if updates > 1:
            self.update_all_button.configure(text="Update all (%d)" % updates)
            self.update_all_button.pack(anchor="e", padx=10, pady=(5, 0), before=self.status)
        else:
            self.update_all_button.pack_forget()
        interrupted = any(
            json.loads(journal.read_text())["status"] == "applying"
            for journal in self.store.home.glob("transactions/*/journal.json")
        )
        if interrupted:
            self.recovery_button.pack(side="left")
        else:
            self.recovery_button.pack_forget()
        for installed, latest in entries:
            package = installed or latest
            key = source_key(package["source"])
            if query and query not in json.dumps(package).lower():
                continue
            card = self.ctk.CTkFrame(
                self.scroll, fg_color=self.namespace["SURFACE_COLOR"], corner_radius=8
            )
            card.pack(fill="x", padx=5, pady=6)
            card.grid_columnconfigure(1, weight=1)
            changed = bool(installed and latest and game_mods.package_changed(installed, latest))
            icon_color = (
                self.namespace["WARNING_COLOR"]
                if changed
                else self.namespace["SUCCESS_COLOR"] if installed else self.namespace["TEXT_MUTED"]
            )
            self.theme.create_label(
                card, text="📦", font=("Segoe UI", 32), text_color=icon_color
            ).grid(row=0, column=0, padx=(20, 15), pady=20, sticky="n")
            info = self.ctk.CTkFrame(card, fg_color="transparent")
            info.grid(row=0, column=1, sticky="nsew", padx=(0, 15), pady=15)
            self.theme.create_label(info, text=package["name"], font=("Segoe UI", 16, "bold")).pack(
                anchor="w"
            )
            if installed:
                controls = self.ctk.CTkFrame(card, fg_color="transparent")
                controls.grid(row=0, column=2, sticky="e", padx=(0, 20), pady=20)
                enabled_var = self.namespace["tk"].BooleanVar(value=installed.get("enabled", True))
                switch = self.ctk.CTkSwitch(
                    controls,
                    text="Enabled",
                    variable=enabled_var,
                    width=100,
                    command=lambda p=installed, v=enabled_var: self.toggle(p, v.get()),
                    font=("Segoe UI", 12),
                    text_color=self.namespace["TEXT_MAIN"],
                    progress_color=self.namespace["SUCCESS_COLOR"],
                    state="disabled" if self.busy else "normal",
                )
                switch.pack(side="left", padx=(0, 12))
                delete_button = self.theme.create_button(
                    controls,
                    text="🗑",
                    font=("Segoe UI Emoji", 14),
                    width=32,
                    height=32,
                    corner_radius=16,
                    appearance="destructive",
                    command=lambda p=installed: self.remove(p),
                    state="disabled" if self.busy else "normal",
                )
                delete_button.pack(side="left")
                self.namespace["CTkToolTip"](delete_button, "Uninstall Mod")
            state_text = (
                "Enabled"
                if installed and installed.get("enabled", True)
                else "Disabled" if installed else "Not installed"
            )
            details = "Installed: " + installed["version"] if installed else "Not installed"
            if latest:
                details += "  |  Latest: " + latest["version"]
                if (
                    installed
                    and not game_mods.package_changed(installed, latest)
                    and key not in self.errors
                ):
                    details += "  |  Up to date"
            if installed and installed.get("disabled_files"):
                state_text += " · %d of %d files selected" % (
                    len(installed["files"]) - len(installed["disabled_files"]),
                    len(installed["files"]),
                )
            details += "  |  " + state_text + "  |  %d MPQ files" % len(package["files"])
            needs_repair = False
            if installed:
                try:
                    bad = [
                        i["dest"]
                        for i in installed["files"]
                        if not self.store.installed(i).exists()
                        or self.store.installed(i).stat().st_size != i["size"]
                    ]
                    if bad:
                        needs_repair = True
                        details += "  |  Needs repair"
                except Exception:
                    details += "  |  File conflict: use inventory"
            self.theme.create_label(
                info, text=details, font=("Segoe UI", 11), text_color=self.namespace["TEXT_MUTED"]
            ).pack(anchor="w", pady=(2, 8))
            if key in self.errors:
                self.theme.create_label(
                    info,
                    text=self.errors[key],
                    text_color=self.namespace["WARNING_COLOR"],
                    wraplength=600,
                    justify="left",
                ).pack(anchor="w")
            buttons = self.ctk.CTkFrame(card, fg_color="transparent")
            buttons.grid(row=1, column=0, columnspan=3, sticky="ew", padx=15, pady=(0, 15))
            self.add_file_disclosure(card, buttons, package)
            actions = []
            if package["source"]["kind"] != "manual":
                actions += [
                    (
                        "Source",
                        lambda p=package: webbrowser.open(
                            p["source"].get("url")
                            or "https://github.com/" + p["source"].get("repo", "")
                        ),
                    )
                ]
            primary_action = None
            if latest and key not in self.errors:
                if not installed:
                    primary_action = ("Install", lambda p=latest: self.install(p))
                elif changed:
                    primary_action = ("Update", lambda p=latest: self.install(p))
                elif needs_repair:
                    primary_action = ("Repair", lambda p=latest: self.install(p))
                actions.append(
                    ("Download without installing", lambda p=latest: self.install(p, True))
                )
            if installed:
                actions.extend(
                    [
                        ("Verify installed files", lambda p=installed: self.verify(p)),
                    ]
                )
                if latest and key not in self.errors:
                    actions.append(("Repair installation…", lambda p=latest: self.install(p)))
                if installed["source"]["kind"] == "manual":
                    actions.append(
                        ("Update from local files…", lambda p=installed: self.manual_update(p))
                    )
                actions.extend(
                    [
                        ("Change source…", lambda p=installed: self.add_source(p)),
                    ]
                )
            elif source_key(package["source"]) != source_key(HD_SOURCE):
                actions.append(
                    ("Stop tracking source", lambda p=package: self.untrack(p["source"]))
                )
            if primary_action:
                label, command = primary_action
                self.theme.create_button(
                    buttons,
                    text=label,
                    command=command,
                    width=100,
                    appearance="primary",
                    state="disabled" if self.busy else "normal",
                ).pack(side="right", padx=(5, 0))
            self.menu_button(
                buttons,
                "Options",
                actions,
                width=90,
                state="disabled" if self.busy else "normal",
            ).pack(side="right")
        for key, error in self.errors.items():
            if key not in installed_sources and key not in self.latest:
                self.theme.create_label(
                    self.scroll,
                    text=error,
                    wraplength=650,
                    justify="left",
                    text_color=self.namespace["WARNING_COLOR"],
                ).pack(anchor="w", padx=12, pady=10)
        if not entries:
            self.theme.create_label(
                self.scroll,
                text="No managed mods yet. Import an MPQ/ZIP or track an update source.",
            ).pack(pady=30)
        self.sidebar("  • " + str(updates) if updates else "")

    def show_report(self, title, text):
        self.report = (title, text)
        self.render()

    def close_report(self):
        self.report = None
        self.render()

    def render_report(self):
        title, text = self.report
        frame = self.ctk.CTkFrame(
            self.scroll, fg_color=self.namespace["SURFACE_COLOR"], corner_radius=8
        )
        frame.pack(fill="both", expand=True, padx=5, pady=6)
        header = self.ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", padx=15, pady=15)
        self.theme.create_label(header, text=title, font=("Segoe UI", 16, "bold")).pack(side="left")
        self.theme.create_button(
            header, text="Back to mods", command=self.close_report, width=110
        ).pack(side="right")
        box = self.theme.create_textbox(frame, wrap="word", height=420)
        box.pack(fill="both", expand=True, padx=15, pady=(0, 15))
        box.insert("1.0", text)
        box.configure(state="disabled")

    def add_file_disclosure(self, card, buttons, package):
        """Expand files in place, preserving the choice across background refreshes."""
        key = (str(self.root), package["id"])
        expanded = key in self.expanded_mods
        file_frame = None
        button = self.theme.create_button(
            buttons,
            text="",
            width=110,
            fg_color="transparent",
            hover_color=self.namespace["CARD_COLOR"],
            anchor="w",
        )
        button.pack(side="left")

        def update_label():
            arrow = "▾" if key in self.expanded_mods else "▸"
            button.configure(text="%s Files (%d)" % (arrow, len(package["files"])))

        def toggle_files():
            nonlocal file_frame
            if key in self.expanded_mods:
                self.expanded_mods.remove(key)
                if file_frame is not None:
                    file_frame.grid_remove()
            else:
                self.expanded_mods.add(key)
                if file_frame is None:
                    file_frame = self.render_file_list(card, package)
                else:
                    file_frame.grid()
            update_label()

        button.configure(command=toggle_files)
        update_label()
        if expanded:
            file_frame = self.render_file_list(card, package)

    def render_file_list(self, card, package):
        frame = self.ctk.CTkFrame(card, fg_color=self.namespace["BG_COLOR"], corner_radius=6)
        frame.grid(row=2, column=0, columnspan=3, sticky="ew", padx=15, pady=(0, 15))
        frame.grid_columnconfigure(0, weight=1)
        source = package["source"]
        source_name = source.get("url") or source.get("repo")
        if source["kind"] == "manual":
            source_name = "Local files"
        elif not source_name:
            source_name = "OctoWoW Standard HD"
        self.theme.create_label(
            frame,
            text="Source: " + source_name,
            text_color=self.namespace["TEXT_MUTED"],
            wraplength=600,
            justify="left",
            anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="ew", padx=12, pady=(10, 8))
        installed = package["id"] in self.store.state()["mods"]
        grouped = source["kind"] == "hd"
        for row, item in enumerate(package["files"], start=1):
            name = Path(item["dest"]).name
            description = (
                game_mods.NAMES.get(name[6].upper(), "") if package["id"] == "hd-standard" else ""
            )
            self.theme.create_label(
                frame,
                text=name + (" — " + description if description else ""),
                anchor="w",
                justify="left",
                wraplength=500,
            ).grid(row=row, column=0, sticky="ew", padx=(12, 8), pady=3)
            self.theme.create_label(
                frame,
                text="%.1f MiB" % (item["size"] / 1024**2),
                text_color=self.namespace["TEXT_MUTED"],
                anchor="e",
            ).grid(row=row, column=1, sticky="e", padx=(0, 12), pady=3)
            if installed and not grouped:
                enabled_var = self.namespace["tk"].BooleanVar(
                    value=item["dest"] not in package.get("disabled_files", [])
                )
                self.ctk.CTkSwitch(
                    frame,
                    text="",
                    width=40,
                    variable=enabled_var,
                    command=lambda p=package, d=item["dest"], v=enabled_var: self.toggle_file(
                        p, d, v.get()
                    ),
                    progress_color=self.namespace["SUCCESS_COLOR"],
                    state="disabled" if self.busy or not package.get("enabled", True) else "normal",
                ).grid(row=row, column=2, padx=(0, 12), pady=3)
        if not grouped:
            self.theme.create_label(
                frame,
                text="File choices are kept when updating this mod. Close WoW before changing switches.",
                text_color=self.namespace["TEXT_MUTED"],
                wraplength=600,
                justify="left",
                anchor="w",
            ).grid(
                row=len(package["files"]) + 1,
                column=0,
                columnspan=3,
                sticky="ew",
                padx=12,
                pady=(8, 10),
            )
        return frame

    def untrack(self, source):
        try:
            with self.store.lock("tracking source"):
                state = self.store.state()
                state["sources"] = [
                    s for s in state["sources"] if source_key(s) != source_key(source)
                ]
                self.store.save(state)
            self.latest.pop(source_key(source), None)
            self.errors.pop(source_key(source), None)
            self.render()
        except Exception as exc:
            self.messagebox.showerror("Source", str(exc))

    def update_all(self):
        try:
            state = self.store.state()
            packages = [
                self.latest[source_key(m["source"])]
                for m in state["mods"].values()
                if source_key(m["source"]) in self.latest
                and source_key(m["source"]) not in self.errors
                and game_mods.package_changed(m, self.latest[source_key(m["source"])])
            ]
            if not packages:
                self.status.configure(
                    text="No checked updates are available. Use Check updates to refresh sources."
                )
                return
            previews = [self.store.preview(p) for p in packages]
            destinations = [i["dest"] for p in packages for i in p["files"]]
            if len(destinations) != len(set(destinations)):
                raise game_mods.ModError(
                    "The selected updates compete for MPQ filenames. Review them separately."
                )
            summary = "\n".join(p["name"] + " -> " + p["version"] for p in packages)
            hosts = sorted({h for p in previews for h in p["new_hosts"]})
            if not self.messagebox.askyesno(
                "Review batch update",
                summary
                + "\n\nNew download hosts: "
                + (", ".join(hosts) or "none")
                + "\nEach mod has its own recoverable operation. If one fails, earlier completed updates remain installed.\nClose WoW before applying. Continue?",
            ):
                return
            store = self.store

            def work(progress):
                completed = []
                for package in packages:
                    try:
                        store.apply(
                            package,
                            game_mods.fetcher(store),
                            approve=True,
                            cancel=self.cancel,
                            progress=progress,
                        )
                        completed.append(package["name"])
                    except Exception as exc:
                        raise game_mods.ModError(
                            "Completed: "
                            + (", ".join(completed) or "none")
                            + ". Stopped at "
                            + package["name"]
                            + ": "
                            + str(exc)
                        ) from exc
                return completed

            self.task("Applying reviewed updates...", work)
        except Exception as exc:
            self.messagebox.showerror("Batch update", str(exc))

    def inventory(self):
        try:
            rows = self.store.inventory()
            text = "\n".join(
                "%s | %s | %s | %.1f MiB"
                % (r["location"], r["name"], r["kind"], r["size"] / 1024**2)
                for r in rows
            )
            text += "\n\nServer files are protected. Manual files are not changed unless you explicitly import them.\nIncomplete downloads are listed only; no automatic cleanup is performed."
            self.show_report("MPQ file inventory", text)
        except Exception as exc:
            self.messagebox.showerror("Inventory", str(exc))

    def inspect_overlaps(self):
        if not self.store:
            return
        store = self.store

        def done(result):
            hits, unknown = result
            self.show_report(
                "Overlapping MPQ assets",
                "\n".join(hits or ["No indexed overlaps found between the inspected mods."])
                + "\n\nCould not inspect:\n"
                + "\n".join(unknown or ["none"])
                + "\n\nThis compares classic MPQ filename hashes, not game behavior. Shared entries can be intentional. "
                "Files inside the same managed bundle are excluded because its fixes intentionally overlap. "
                "An empty report does not prove compatibility. Base/server archives are excluded.",
            )

        self.task(
            "Inspecting MPQ indexes (read only)...",
            lambda progress: mpq_inspect.overlaps(store.inventory()),
            done,
        )

    def approve(self, package, migrate=False):
        preview = self.store.preview(package, migrate)
        lines = [
            package["name"] + " " + package["version"],
            "Files: " + ", ".join(Path(i["dest"]).name for i in package["files"]),
            "Replaces untracked files: " + (", ".join(preview["replacements"]) or "none"),
            "Removes old mod files: " + (", ".join(preview["removed_files"]) or "none"),
            "New download hosts: " + (", ".join(preview["new_hosts"]) or "none"),
            "Source: " + json.dumps(package["source"]),
            "Only replaced files are retained for recovery. Unrelated MPQs remain unchanged.",
        ]
        if migrate:
            lines.append("You are changing this mod's trusted update source.")
        return self.messagebox.askyesno("Review mod changes", "\n\n".join(lines))

    def install(self, package, download_only=False, local=None, migrate=False):
        try:
            if not self.approve(package, migrate):
                return
            store = self.store
            fetch = (lambda item, *a: local[item["dest"]]) if local else game_mods.fetcher(store)
            self.task(
                "Downloading mod files..." if download_only else "Applying mod...",
                lambda progress: store.apply(
                    package,
                    fetch,
                    approve=True,
                    cancel=self.cancel,
                    progress=progress,
                    download_only=download_only,
                    migrate_source=migrate,
                ),
                lambda result: self.status.configure(
                    text=(
                        "Download verified. Close WoW, then choose Apply / repair."
                        if download_only
                        else "Mod applied. Restart WoW to load the changes."
                    )
                ),
            )
        except Exception as exc:
            self.messagebox.showerror("Mod changes", str(exc))

    def import_dialog(self):
        paths = self.dialog.askopenfilenames(
            title="Import MPQ mod", filetypes=[("MPQ mod or ZIP", "*.mpq *.zip")]
        )
        if paths:
            self.import_paths(paths)

    def manual_update(self, installed):
        paths = self.dialog.askopenfilenames(
            title="Select replacement MPQs or ZIP", filetypes=[("MPQ mod or ZIP", "*.mpq *.zip")]
        )
        if paths:
            self.import_paths(paths, installed)

    def import_paths(self, paths, installed=None):
        name = (
            installed["name"]
            if installed
            else self.ctk.CTkInputDialog(
                text="Name for this mod or bundle:", title="Import mod"
            ).get_input()
        )
        if not name:
            return
        root = self.root

        def done(result):
            package, local = result
            if installed:
                package["id"] = installed["id"]
            self.install(package, local=local)

        self.task(
            "Reading imported files...",
            lambda progress: game_mods.import_package(paths, root, name),
            done,
        )

    def add_source(self, installed=None):
        if self.busy:
            return
        dialog = self.ctk.CTkInputDialog(
            text="GitHub repository URL or HTTPS game-mod manifest URL:", title="Track MPQ source"
        )
        url = dialog.get_input()
        if not url:
            return
        try:
            source = game_mods.source_from_url(url.strip())
            if installed:
                source["target_id"] = installed["id"]
        except Exception as exc:
            self.messagebox.showerror("Source", str(exc))
            return

        def done(package):
            if installed:
                package["id"] = installed["id"]
                self.install(package, migrate=True)
            else:
                if not self.approve(package):
                    return
                with self.store.lock("tracking source"):
                    state = self.store.state()
                    if source_key(source) not in {source_key(s) for s in state["sources"]}:
                        state["sources"].append(source)
                        self.store.save(state)
                self.latest[source_key(source)] = package
                self.render()

        self.task(
            "Reading update source...",
            lambda progress: game_mods.source_package(source, hd_source),
            done,
        )

    def verify(self, package):
        store = self.store
        self.task(
            "Verifying " + package["name"] + "...",
            lambda progress: store.verify(package["id"]),
            lambda bad: self.status.configure(
                text=(
                    "Verification passed: all file hashes match."
                    if not bad
                    else "Changed or missing files: " + ", ".join(bad) + ". Use Apply / repair."
                )
            ),
        )

    def toggle(self, package, enabled):
        store = self.store
        self.task("Changing mod state...", lambda progress: store.toggle(package["id"], enabled))

    def toggle_file(self, package, file_dest, enabled):
        store = self.store
        self.task(
            "Changing file state...",
            lambda progress: store.toggle_file(package["id"], file_dest, enabled),
        )

    def remove(self, package):
        store = self.store
        if self.messagebox.askyesno(
            "Remove mod",
            "Remove "
            + package["name"]
            + "?\nOnly owned files are removed. Replaced originals are restored. Close WoW first.",
        ):
            self.task("Removing mod...", lambda progress: store.remove(package["id"]))

    def recover(self):
        if self.store:
            store = self.store
            self.task("Recovering interrupted work...", lambda progress: store.recover())

    def undo(self):
        if self.store and self.messagebox.askyesno(
            "Restore last change", "Restore the previous mod files and records? Close WoW first."
        ):
            store = self.store
            self.task("Restoring last change...", lambda progress: store.undo())

    def restore_records(self):
        if self.store and self.messagebox.askyesno(
            "Restore mod records",
            "Restore the previous valid mod records? Game files are not changed.",
        ):
            store = self.store
            self.task("Restoring records...", lambda progress: game_mods.restore_records(store))

    def can_close(self):
        if self.busy or (self.store and self.store.locked()):
            self.messagebox.showinfo(
                "Operation in progress",
                "Pause the download and wait for the operation to stop before closing.",
            )
            return False
        return True

    def poll(self):
        try:
            while True:
                kind, root, value = self.events.get_nowait()
                if root != self.root:
                    continue
                if kind == "progress":
                    text, fraction = value
                    self.status.configure(text=text)
                    if text.startswith("Applying verified files."):
                        self.pause_button.configure(state="disabled")
                    if fraction is not None:
                        self.progress.set(fraction)
                elif kind == "error":
                    self.set_busy(False)
                    self.status.configure(text=str(value))
                    if Path(self.app.wow_dir.get().strip()).resolve() != self.root:
                        self.refresh()
                else:
                    result, done, label = value
                    self.set_busy(False)
                    self.progress.set(1)
                    self.status.configure(text=label.rstrip(".") + " finished.")
                    if done:
                        done(result)
                    if not self.busy:
                        if Path(self.app.wow_dir.get().strip()).resolve() != self.root:
                            self.refresh()
                        else:
                            self.render()
        except queue.Empty:
            pass
        except Exception as exc:
            self.busy = False
            self.status.configure(text="Mod records need attention: " + str(exc))
        finally:
            self.app.after(150, self.poll)
