"""Installation-scoped MPQ ownership, source adapters and recoverable operations."""

import copy
import hashlib
import json
import msvcrt
import os
import re
import shutil
import struct
import time
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

# --- MOD CONFIGURATION ---
SCHEMA = 1
MOD_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z")
PATCH = re.compile(r"patch-[a-z]\.mpq\Z", re.I | re.ASCII)
CORE = {
    "backup.mpq",
    "base.mpq",
    "dbc.mpq",
    "fonts.mpq",
    "interface.mpq",
    "misc.mpq",
    "model.mpq",
    "patch.mpq",
    "sound.mpq",
    "speech.mpq",
    "terrain.mpq",
    "texture.mpq",
    "wmo.mpq",
}
NAMES = dict(
    zip(
        "ABCDEGIMPSTOFHZ",
        [
            "Characters & NPCs",
            "Buildings",
            "Creatures",
            "Objects & foliage",
            "Environment",
            "Gear & weapons",
            "Interface",
            "Maps",
            "Spell effects",
            "Sounds & music",
            "Character textures",
            "Raid visuals",
            "Character model fixes",
            "OctoWoW HD fixes",
            "Merged compatibility tables",
        ],
    )
)
GITHUB_HOSTS = {
    "github.com",
    "api.github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}


class ModError(Exception):
    pass


class Cancelled(ModError):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + os.urandom(16).hex() + ".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def destination(value):
    if not isinstance(value, str) or "\\" in value:
        raise ModError("Use a Data/patch-X.mpq destination.")
    parts = value.split("/")
    if len(parts) != 2 or parts[0].lower() != "data" or not PATCH.fullmatch(parts[1]):
        raise ModError("Only lettered Data/patch-X.mpq mod files can be managed.")
    return "Data/" + parts[1].lower()


def host(url):
    parsed_url = urlparse(url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
    ):
        raise ModError("Sources and downloads must use HTTPS without embedded credentials.")
    return parsed_url.hostname.lower()


class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if host(newurl) not in self.hosts:
            raise ModError(
                "Download redirected to a new host; review the source first: " + host(newurl)
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url, hosts, headers=None):
    if host(url) not in hosts:
        raise ModError("New download host requires source review: " + host(url))
    request = urllib.request.Request(
        url, headers={"User-Agent": "OctoWoW-GameMods/2", **(headers or {})}
    )
    return urllib.request.build_opener(CheckedRedirect(hosts)).open(request, timeout=30)


def json_url(url, hosts):
    with open_url(url, hosts) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ModError("Source metadata is unexpectedly large.")
    return json.loads(data.decode("utf-8"))


def validate_package(package):
    validated_package = copy.deepcopy(package)
    if not MOD_ID.fullmatch(validated_package.get("id", "")):
        raise ModError("Invalid mod identity.")
    if not isinstance(validated_package.get("name"), str) or not validated_package["name"].strip():
        raise ModError("The source must name the mod.")
    if (
        not isinstance(validated_package.get("version"), str)
        or not 0 < len(validated_package["version"]) < 100
    ):
        raise ModError("Invalid version.")
    if (
        not isinstance(validated_package.get("files"), list)
        or not 0 < len(validated_package["files"]) <= 26
    ):
        raise ModError("A mod must contain between 1 and 26 MPQs.")
    seen = set()
    for item in validated_package["files"]:
        item["dest"] = destination(item["dest"])
        if item["dest"] in seen:
            raise ModError("The mod contains duplicate destinations.")
        seen.add(item["dest"])
        if not re.fullmatch("[0-9a-f]{64}", item.get("sha256", "")):
            raise ModError("Automatic updates require a published SHA-256 for every MPQ.")
        if type(item.get("size")) is not int or not 0 < item["size"] <= 8 * 1024**3:
            raise ModError("Invalid MPQ download size.")
        if "url" in item:
            host(item["url"])
    if not isinstance(validated_package.get("requires", []), list):
        raise ModError("Mod dependencies must be a list of mod identities.")
    for dependency in validated_package.get("requires", []):
        if (
            not isinstance(dependency, str)
            or not MOD_ID.fullmatch(dependency)
            or dependency == validated_package["id"]
        ):
            raise ModError("Invalid mod dependency.")
    if not isinstance(validated_package.get("requires_dlls", []), list) or any(
        not isinstance(d, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.dll", d)
        for d in validated_package.get("requires_dlls", [])
    ):
        raise ModError("Invalid client-plugin requirements.")
    return validated_package


def package_changed(installed, latest):
    def signature(p):
        return sorted((destination(i["dest"]), i["sha256"], i["size"]) for i in p["files"])

    return (
        installed["version"] != latest["version"]
        or signature(installed) != signature(latest)
        or bool(
            {host(i["url"]) for i in latest["files"] if "url" in i}
            - set(installed.get("approved_hosts", []))
        )
    )


def source_package(source, hd=None):
    def finish(package):
        if source.get("target_id"):
            package["id"] = source["target_id"]
        package["source"] = source
        return validate_package(package)

    kind = source["kind"]
    if kind == "hd":
        manifest = hd.latest_release()
        files = hd.validate_manifest(manifest)["files"]
        return finish(
            {
                "id": "hd-standard",
                "name": "Project Reforged HD (OctoWoW)",
                "version": manifest["release"],
                "files": files,
                "requires": [],
                "source": source,
                "requires_dlls": ["VanillaHelpers.dll"],
                "requires_laa": True,
            }
        )
    if kind == "manifest":
        obj = json_url(source["url"], {host(source["url"])} | set(source.get("metadata_hosts", [])))
        if obj.get("schema") != SCHEMA:
            raise ModError("Unsupported manifest format; installed files are unchanged.")
        return finish(obj)
    if kind == "github":
        repo = source["repo"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ModError("Use a GitHub owner/repository URL.")
        release = json_url(
            "https://api.github.com/repos/" + repo + "/releases/latest", GITHUB_HOSTS
        )
        files = []
        for asset in release.get("assets", []):
            if PATCH.fullmatch(asset["name"]):
                digest = asset.get("digest") or ""
                files.append(
                    {
                        "dest": "Data/" + asset["name"],
                        "size": asset["size"],
                        "sha256": digest.removeprefix("sha256:"),
                        "url": asset["browser_download_url"],
                    }
                )
        package = {
            "id": source["id"],
            "name": source.get("name", repo),
            "version": release["tag_name"],
            "files": files,
            "source": source,
        }
        return finish(package)
    raise ModError("This manually imported mod has no automatic update source.")


def source_from_url(url):
    source_host = host(url)
    parsed_url = urlparse(url)
    if source_host == "github.com":
        bits = parsed_url.path.strip("/").removesuffix(".git").split("/")
        if len(bits) != 2:
            raise ModError("Use the repository home URL, not a branch or file URL.")
        repo = "/".join(bits)
        if repo.lower() == "fmustafayaman/octowow-hd-switch":
            return {"kind": "hd", "url": url}
        return {
            "kind": "github",
            "repo": repo,
            "id": "github-" + hashlib.sha256(repo.lower().encode()).hexdigest()[:16],
        }
    return {"kind": "manifest", "url": url}


class Store:
    def __init__(self, root, running=lambda: False):
        self.root = Path(root).resolve()
        if not (self.root / "WoW.exe").is_file() or not (self.root / "Data").is_dir():
            raise ModError("Choose an existing WoW installation.")
        if (self.root / "Data").is_symlink() or (
            self.root / "Data"
        ).resolve() != self.root / "Data":
            raise ModError("The Data folder must be inside this installation.")
        self.home = self.root / ".octowow-mods"
        if self.home.is_symlink() or self.home.resolve().parent != self.root:
            raise ModError("Mod records must be inside this installation.")
        self.running = running
        self.state_path = self.home / "registry.json"

    def state(self):
        if not self.state_path.exists():
            return {"schema": SCHEMA, "mods": {}, "sources": []}
        try:
            state = json.loads(self.state_path.read_text("utf-8"))
            if state.get("schema") != SCHEMA or not isinstance(state.get("mods"), dict):
                raise ValueError("schema")
            hd = state["mods"].get("hd-standard")
            if hd and hd["source"]["kind"] == "hd":
                hd.setdefault("requires_dlls", ["VanillaHelpers.dll"])
                hd.setdefault("requires_laa", True)
            return state
        except (ValueError, OSError) as exc:
            raise ModError(
                "Mod records cannot be read. Use Restore records; no game files were changed."
            ) from exc

    def save(self, state):
        if self.state_path.exists():
            previous = self.state_path.read_bytes()
            # Never replace the recovery copy with corrupt input.
            json.loads(previous)
            atomic_json(self.home / "registry.previous.json", json.loads(previous))
        atomic_json(self.state_path, state)

    @contextmanager
    def lock(self, phase="preparing"):
        self.home.mkdir(exist_ok=True)
        with open(self.home / "operation.lock", "a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ModError("Another manager operation is using this installation.") from exc
            try:
                atomic_json(self.home / "operation.json", {"pid": os.getpid(), "phase": phase})
                if phase not in ("recovering", "checking", "verifying", "restoring records"):
                    pending = [
                        p
                        for p in self.home.glob("transactions/*/journal.json")
                        if json.loads(p.read_text("utf-8"))["status"] == "applying"
                    ]
                    if pending:
                        raise ModError("Recover the interrupted operation before changing mods.")
                yield
            finally:
                (self.home / "operation.json").unlink(missing_ok=True)
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def locked(self):
        try:
            with self.lock("checking"):
                return False
        except ModError:
            return True

    def path(self, relative):
        disabled = relative.lower().endswith(".disabled")
        canonical = destination(relative[:-9] if disabled else relative)
        name = Path(canonical).name + (".disabled" if disabled else "")
        matches = [p for p in (self.root / "Data").iterdir() if p.name.lower() == name.lower()]
        path = matches[0] if matches else self.root / "Data" / name
        if path.is_symlink() or path.resolve().parent != self.root / "Data":
            raise ModError("MPQ path leaves the game Data folder.")
        return path

    def installed(self, item):
        enabled = self.path(item["dest"])
        disabled = self.path(item["dest"] + ".disabled")
        if enabled.exists() and disabled.exists():
            raise ModError("Enabled and disabled copies both exist: " + enabled.name)
        return disabled if disabled.exists() else enabled

    def original(self, value, canonical):
        record = value if isinstance(value, dict) else {"backup": value, "dest": canonical}
        backup = self.home / record["backup"]
        if (
            not backup.resolve().is_relative_to((self.home / "originals").resolve())
            or not backup.is_file()
        ):
            raise ModError("The original-file recovery copy is missing or invalid.")
        self.path(record["dest"])
        return record["dest"], backup

    def inventory(self):
        state = self.state()
        owners = {
            f["dest"].lower(): mod_id for mod_id, mod in state["mods"].items() for f in mod["files"]
        }
        result = []
        for folder in (self.root / "Data", self.root):
            for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
                if not path.is_file() or ".mpq" not in path.name.lower():
                    continue
                name = path.name.lower()
                base = name.removesuffix(".disabled")
                kind = (
                    "incomplete"
                    if name.endswith((".part", ".aria2"))
                    else (
                        "server"
                        if base in CORE or re.fullmatch(r"patch-\d+\.mpq", base)
                        else (
                            "managed"
                            if "data/" + base in owners and folder == self.root / "Data"
                            else "manual"
                        )
                    )
                )
                result.append(
                    {
                        "name": path.name,
                        "path": str(path),
                        "kind": kind,
                        "owner": (
                            owners.get("data/" + base) if folder == self.root / "Data" else None
                        ),
                        "enabled": not name.endswith(".disabled"),
                        "size": path.stat().st_size,
                        "location": "Data" if folder == self.root / "Data" else "Game folder",
                    }
                )
        return result

    def guard(self):
        if self.running():
            raise ModError(
                "Close WoW before changing game files. Downloads can continue while playing."
            )

    def recover(self):
        with self.lock("recovering"):
            self.guard()
            for journal in sorted(self.home.glob("transactions/*/journal.json")):
                record = json.loads(journal.read_text("utf-8"))
                if record["status"] not in ("committed", "rolled_back"):
                    self._rollback(journal.parent, record)

    def _rollback(self, folder, record):
        if record.get("format") == 2:
            for operation in record["operations"]:
                current = self.path(operation["dest"])
                if current.exists() and sha256(current) not in {
                    operation.get("new_sha"),
                    operation.get("before_sha"),
                }:
                    raise ModError(
                        "A file changed outside the interrupted operation; preserve it for review: "
                        + operation["dest"]
                    )
        for operation in reversed(record["operations"]):
            dest = self.path(operation["dest"])
            if not re.fullmatch(r"\d+\.mpq", operation["backup"]):
                raise ModError("Invalid recovery backup path; no files restored.")
            saved = folder / operation["backup"]
            if saved.exists():
                os.replace(saved, dest)
            elif not operation["existed"]:
                dest.unlink(missing_ok=True)
            dest.with_name(dest.name + ".installing").unlink(missing_ok=True)
        atomic_json(self.state_path, record["before"])
        record["status"] = "rolled_back"
        atomic_json(folder / "journal.json", record)

    def transact(self, changes, after, label):
        """Caller holds the installation lock. Journal before every file change."""
        self.guard()
        before = self.state()
        folder = self.home / "transactions" / os.urandom(16).hex()
        folder.mkdir(parents=True)
        record = {
            "format": 2,
            "status": "applying",
            "label": label,
            "before": before,
            "operations": [],
        }
        atomic_json(folder / "journal.json", record)
        atomic_json(self.home / "operation.json", {"pid": os.getpid(), "phase": "applying"})
        try:
            for relative, src in changes.items():
                self.guard()
                target = self.path(relative)
                operation = {
                    "dest": relative,
                    "backup": str(len(record["operations"])) + ".mpq",
                    "existed": target.exists(),
                    "before_sha": sha256(target) if target.exists() else None,
                    "new_sha": sha256(src) if src is not None else None,
                }
                record["operations"].append(operation)
                atomic_json(folder / "journal.json", record)
                if target.exists():
                    os.replace(target, folder / operation["backup"])
                if src is not None:
                    # Keep verified staging files available for retry and recovery.
                    temp = target.with_name(target.name + ".installing")
                    shutil.copyfile(src, temp)
                    with open(temp, "r+b") as sync:
                        os.fsync(sync.fileno())
                    os.replace(temp, target)
            self.guard()
            self.save(after)
            record["after_files"] = {
                relative: sha256(self.path(relative)) if self.path(relative).exists() else None
                for relative in changes
            }
            record["status"] = "committed"
            atomic_json(folder / "journal.json", record)
        except BaseException:
            self._rollback(folder, record)
            raise
        return folder

    def preview(self, package, migrate_source=False):
        package = validate_package(package)
        state = self.state()
        old = state["mods"].get(package["id"])
        if package["source"]["kind"] == "hd" and old and old.get("disabled_files"):
            raise ModError("Re-enable individual files before switching to the grouped HD source.")
        if old and old["source"] != package["source"] and not migrate_source:
            raise ModError("This mod identity belongs to another source.")
        owners = {f["dest"]: mid for mid, mod in state["mods"].items() for f in mod["files"]}
        for item in package["files"]:
            if owners.get(item["dest"]) not in (None, package["id"]):
                raise ModError(
                    item["dest"]
                    + " is owned by "
                    + owners[item["dest"]]
                    + ". Remove that mod first."
                )
        missing = [
            d
            for d in package.get("requires", [])
            if d not in state["mods"]
            or not state["mods"][d].get("enabled", True)
            or state["mods"][d].get("disabled_files")
        ]
        if missing:
            raise ModError("Install and enable required mods first: " + ", ".join(missing))
        loader = self.root / "dlls.txt"
        loaded = (
            {
                line.split("#", 1)[0].strip().lower()
                for line in loader.read_text("utf-8", errors="replace").splitlines()
            }
            if loader.exists()
            else set()
        )
        for dll in package.get("requires_dlls", []):
            if not (self.root / dll).is_file() or dll.lower() not in loaded:
                raise ModError("Install and enable the required client plugin first: " + dll)
        if package.get("requires_laa"):
            executable = self.root / (
                "WoW_Tweaked.exe" if (self.root / "WoW_Tweaked.exe").exists() else "WoW.exe"
            )
            with open(executable, "rb") as f:
                f.seek(60)
                raw = f.read(4)
                if len(raw) != 4:
                    raise ModError("Cannot check the game memory-limit patch.")
                f.seek(struct.unpack("<I", raw)[0] + 22)
                flags = f.read(2)
            if len(flags) != 2 or not struct.unpack("<H", flags)[0] & 0x20:
                raise ModError(
                    "Enable Large Address Aware in Game Settings before installing this mod."
                )
        collisions = [
            i["dest"]
            for i in package["files"]
            if self.installed(i).exists() and owners.get(i["dest"]) != package["id"]
        ]
        hosts = sorted({host(i["url"]) for i in package["files"] if "url" in i})
        old_hosts = set(old.get("approved_hosts", [])) if old else set()
        old_files = {f["dest"] for f in old["files"]} if old else set()
        return {
            "package": package,
            "replacements": collisions,
            "new_hosts": sorted(set(hosts) - old_hosts),
            "removed_files": sorted(old_files - {f["dest"] for f in package["files"]}),
            "bytes": sum(f["size"] for f in package["files"]),
            "hosts": hosts,
        }

    def apply(
        self,
        package,
        fetch,
        approve=False,
        cancel=None,
        progress=lambda *a: None,
        download_only=False,
        migrate_source=False,
    ):
        with self.lock():
            if any(
                json.loads(p.read_text())["status"] == "applying"
                for p in self.home.glob("transactions/*/journal.json")
            ):
                raise ModError("Recover the interrupted operation before installing.")
            preview = self.preview(package, migrate_source)
            p = preview["package"]
            if (
                preview["replacements"] or preview["new_hosts"] or preview["removed_files"]
            ) and not approve:
                raise ModError("Review and approve the file/source changes before applying.")
            state = self.state()
            old = state["mods"].get(p["id"])
            if shutil.disk_usage(self.root).free < preview["bytes"] * 2 + 64 * 1024**2:
                raise ModError("Not enough disk space for verified staging and recovery copies.")
            staged = {}
            for item in p["files"]:
                if cancel and cancel.is_set():
                    raise Cancelled("Download cancelled; installed files are unchanged.")
                existing = self.installed(item)
                progress("Checking " + Path(item["dest"]).name)
                if (
                    existing.exists()
                    and existing.stat().st_size == item["size"]
                    and sha256(existing) == item["sha256"]
                ):
                    staged[item["dest"]] = existing
                else:
                    path = fetch(item, set(preview["hosts"]) | GITHUB_HOSTS, cancel, progress)
                    if path.stat().st_size != item["size"] or sha256(path) != item["sha256"]:
                        raise ModError("Integrity check failed: " + item["dest"])
                    staged[item["dest"]] = path
            if cancel and cancel.is_set():
                raise Cancelled("Download paused; installed files are unchanged.")
            if download_only:
                return p
            self.guard()
            changes = {}
            originals = copy.deepcopy(old.get("originals", {})) if old else {}
            baseline_dir = self.home / "originals" / p["id"]
            for item in p["files"]:
                existing = self.installed(item)
                if item["dest"] in preview["replacements"]:
                    baseline_dir.mkdir(parents=True, exist_ok=True)
                    saved = baseline_dir / Path(item["dest"]).name
                    if not saved.exists():
                        shutil.copy2(existing, saved)
                    originals[item["dest"]] = {
                        "backup": str(saved.relative_to(self.home)),
                        "dest": str(existing.relative_to(self.root)).replace("\\", "/"),
                    }
                enabled = (old.get("enabled", True) if old else True) and item["dest"] not in (
                    old.get("disabled_files", []) if old else []
                )
                desired = item["dest"] + ("" if enabled else ".disabled")
                if staged[item["dest"]] != self.path(desired):
                    if existing.exists() and existing != self.path(desired):
                        changes[str(existing.relative_to(self.root)).replace("\\", "/")] = None
                    changes[desired] = staged[item["dest"]]
            for item in old["files"] if old else []:
                if item["dest"] not in staged:
                    previous = self.installed(item)
                    rel = str(previous.relative_to(self.root)).replace("\\", "/")
                    original = originals.pop(item["dest"], None)
                    changes[rel] = None
                    if original:
                        original_dest, saved = self.original(original, item["dest"])
                        if (
                            self.path(original_dest).exists()
                            and self.path(original_dest) != previous
                        ):
                            raise ModError(
                                "Original destination now contains another file; review it first."
                            )
                        changes[original_dest] = saved
            p["enabled"] = old.get("enabled", True) if old else True
            p["disabled_files"] = [
                item["dest"]
                for item in p["files"]
                if old and item["dest"] in old.get("disabled_files", [])
            ]
            p["originals"] = originals
            p["approved_hosts"] = preview["hosts"]
            if old and old["source"] != p["source"]:
                state["sources"] = [s for s in state["sources"] if s != old["source"]]
            state["mods"][p["id"]] = p
            progress("Applying verified files. Keep WoW closed until this finishes.", 0)
            self.transact(changes, state, "Install/update " + p["name"])
            return p

    def toggle(self, mod_id, enabled):
        with self.lock():
            self.guard()
            state = self.state()
            mod = state["mods"][mod_id]
            if not enabled:
                dependents = [
                    m["name"]
                    for mid, m in state["mods"].items()
                    if mid != mod_id and mod_id in m.get("requires", []) and m.get("enabled", True)
                ]
                if dependents:
                    raise ModError("Disable dependent mods first: " + ", ".join(dependents))
            else:
                self.preview(mod)
            changes = {}
            for item in mod["files"]:
                path = self.installed(item)
                if not path.exists() or sha256(path) != item["sha256"]:
                    raise ModError(
                        "Verify/repair the changed or missing file before toggling: " + path.name
                    )
                file_enabled = enabled and item["dest"] not in mod.get("disabled_files", [])
                desired = item["dest"] + ("" if file_enabled else ".disabled")
                if path != self.path(desired):
                    # Copy to a separate staged file; transaction can then move originals away.
                    stage = self.home / "toggle-cache" / Path(item["dest"]).name
                    stage.parent.mkdir(exist_ok=True)
                    shutil.copyfile(path, stage)
                    changes[str(path.relative_to(self.root)).replace("\\", "/")] = None
                    changes[desired] = stage
            mod["enabled"] = enabled
            self.transact(changes, state, ("Enable " if enabled else "Disable ") + mod["name"])

    def toggle_file(self, mod_id, file_dest, enabled):
        """Change one owned file while retaining its preference across mod updates."""
        with self.lock():
            self.guard()
            state = self.state()
            mod = state["mods"][mod_id]
            if mod["source"]["kind"] == "hd":
                raise ModError("This HD set shares compatibility tables. Use its whole-mod switch.")
            if not mod.get("enabled", True):
                raise ModError("Enable the mod before changing individual files.")
            file_dest = destination(file_dest)
            item = next((item for item in mod["files"] if item["dest"] == file_dest), None)
            if item is None:
                raise ModError("This file is not owned by the selected mod.")
            if not enabled:
                dependents = [
                    other["name"]
                    for other_id, other in state["mods"].items()
                    if other_id != mod_id
                    and mod_id in other.get("requires", [])
                    and other.get("enabled", True)
                ]
                if dependents:
                    raise ModError("Disable dependent mods first: " + ", ".join(dependents))
            else:
                self.preview(mod)
            path = self.installed(item)
            if not path.exists() or sha256(path) != item["sha256"]:
                raise ModError(
                    "Verify/repair the changed or missing file before toggling: " + path.name
                )
            disabled_files = set(mod.get("disabled_files", []))
            if enabled:
                disabled_files.discard(file_dest)
            else:
                disabled_files.add(file_dest)
            mod["disabled_files"] = sorted(disabled_files)
            desired = file_dest + ("" if enabled else ".disabled")
            changes = {}
            if path != self.path(desired):
                stage = self.home / "toggle-cache" / Path(file_dest).name
                stage.parent.mkdir(exist_ok=True)
                shutil.copyfile(path, stage)
                changes[str(path.relative_to(self.root)).replace("\\", "/")] = None
                changes[desired] = stage
            self.transact(
                changes, state, ("Enable " if enabled else "Disable ") + Path(file_dest).name
            )

    def remove(self, mod_id):
        with self.lock():
            state = self.state()
            dependents = [
                m["name"]
                for mid, m in state["mods"].items()
                if mid != mod_id and mod_id in m.get("requires", [])
            ]
            if dependents:
                raise ModError("Remove dependent mods first: " + ", ".join(dependents))
            mod = state["mods"][mod_id]
            changes = {}
            for item in mod["files"]:
                path = self.installed(item)
                if path.exists() and sha256(path) != item["sha256"]:
                    raise ModError(
                        "This file changed outside the manager; keep it until reviewed: "
                        + path.name
                    )
                rel = str(path.relative_to(self.root)).replace("\\", "/")
                original = mod.get("originals", {}).get(item["dest"])
                changes[rel] = None
                if original:
                    original_dest, saved = self.original(original, item["dest"])
                    if self.path(original_dest).exists() and self.path(original_dest) != path:
                        raise ModError(
                            "Original destination now contains another file; review it first."
                        )
                    changes[original_dest] = saved
            del state["mods"][mod_id]
            self.transact(changes, state, "Remove " + mod["name"])

    def verify(self, mod_id):
        with self.lock("verifying"):
            mod = self.state()["mods"][mod_id]
            return [
                item["dest"]
                for item in mod["files"]
                if not self.installed(item).exists()
                or self.installed(item).stat().st_size != item["size"]
                or sha256(self.installed(item)) != item["sha256"]
            ]

    def undo(self):
        with self.lock("restoring"):
            self.guard()
            candidates = [
                (p, json.loads(p.read_text()))
                for p in self.home.glob("transactions/*/journal.json")
            ]
            candidates = [(p, r) for p, r in candidates if r["status"] == "committed"]
            if not candidates:
                raise ModError("No previous operation is available to restore.")
            path, record = max(candidates, key=lambda pair: pair[0].stat().st_mtime_ns)
            for relative, expected in record.get("after_files", {}).items():
                actual = self.path(relative)
                if (sha256(actual) if actual.exists() else None) != expected:
                    raise ModError(
                        "Files changed since the last operation. Verify them before restoring: "
                        + relative
                    )
            self._rollback(path.parent, record)


def is_mpq(path):
    with open(path, "rb") as f:
        prefix = f.read(4096)
    return prefix.startswith((b"MPQ\x1a", b"MPQ\x1b")) or any(
        prefix[n : n + 4] == b"MPQ\x1a" for n in range(512, len(prefix), 512)
    )


def import_package(paths, root, name):
    """Extract MPQs only, never archive paths, scripts, addons or DLLs."""
    stage = Path(root) / ".octowow-mods" / "imports" / os.urandom(16).hex()
    stage.mkdir(parents=True)
    files, local = [], {}
    total = 0
    for path in map(Path, paths):
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                for entry in archive.infolist():
                    if not entry.filename.lower().endswith(".mpq"):
                        continue
                    if ".." in entry.filename.replace("\\", "/").split(
                        "/"
                    ) or entry.filename.startswith(("/", "\\")):
                        raise ModError("Unsafe archive path.")
                    filename = entry.filename.replace("\\", "/").split("/")[-1]
                    dest = destination("Data/" + filename)
                    total += entry.file_size
                    if total > 32 * 1024**3 or entry.file_size > 8 * 1024**3:
                        raise ModError("Archive exceeds the supported size.")
                    target = stage / Path(dest).name
                    if dest in local:
                        raise ModError("Duplicate MPQ filename in archive.")
                    if entry.file_size + 64 * 1024**2 > shutil.disk_usage(stage).free:
                        raise ModError("Not enough disk space to extract the mod.")
                    with archive.open(entry) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                    local[dest] = target
        else:
            dest = destination("Data/" + path.name)
            if dest in local:
                raise ModError("Duplicate MPQ filename.")
            target = stage / Path(dest).name
            shutil.copyfile(path, target)
            local[dest] = target
    if not local:
        raise ModError(
            "No supported lettered MPQ files were found. Addon ZIPs belong in Addon Manager."
        )
    for dest, target in local.items():
        if not is_mpq(target):
            raise ModError("Not a recognizable MPQ archive: " + target.name)
        files.append({"dest": dest, "size": target.stat().st_size, "sha256": sha256(target)})
    package = validate_package(
        {
            "id": "manual-" + os.urandom(8).hex(),
            "name": name,
            "version": "Manual",
            "source": {"kind": "manual"},
            "files": files,
        }
    )
    return package, local


def fetcher(store):
    def fetch(item, hosts, cancel, progress):
        cache = store.home / "downloads"
        cache.mkdir(parents=True, exist_ok=True)
        final = cache / (item["sha256"] + ".mpq")
        partial = final.with_suffix(".part")
        if (
            final.exists()
            and final.stat().st_size == item["size"]
            and sha256(final) == item["sha256"]
        ):
            return final
        offset = partial.stat().st_size if partial.exists() else 0
        if offset == item["size"] and sha256(partial) == item["sha256"]:
            os.replace(partial, final)
            return final
        if offset >= item["size"]:
            partial.unlink()
            offset = 0
        headers = {"Range": "bytes=%d-" % offset} if offset else {}
        with open_url(item["url"], hosts, headers) as response:
            if offset and (
                response.status != 206
                or not response.headers.get("Content-Range", "").startswith("bytes %d-" % offset)
            ):
                offset = 0
            with open(partial, "ab" if offset else "wb") as out:
                last = 0
                while True:
                    if cancel and cancel.is_set():
                        raise Cancelled(
                            "Download paused. Installed files are unchanged; click Download to resume."
                        )
                    data = response.read(256 * 1024)
                    if not data:
                        break
                    out.write(data)
                    offset += len(data)
                    if offset > item["size"]:
                        raise ModError("Download is larger than the approved file.")
                    if time.monotonic() - last >= 0.3:
                        progress(
                            "%s: %.1f / %.1f MiB"
                            % (Path(item["dest"]).name, offset / 1024**2, item["size"] / 1024**2),
                            offset / item["size"],
                        )
                        last = time.monotonic()
        if partial.stat().st_size != item["size"] or sha256(partial) != item["sha256"]:
            partial.unlink(missing_ok=True)
            raise ModError(
                "Downloaded file does not match the published checksum; installed files are unchanged."
            )
        if not is_mpq(partial):
            partial.unlink(missing_ok=True)
            raise ModError("The downloaded file is not an MPQ archive.")
        os.replace(partial, final)
        return final

    return fetch


def zip_contains_mpq(path):
    with zipfile.ZipFile(path) as archive:
        return any(i.filename.lower().endswith(".mpq") for i in archive.infolist())


def restore_records(store):
    with store.lock("restoring records"):
        store.guard()
        backup = store.home / "registry.previous.json"
        value = json.loads(backup.read_text("utf-8"))
        if value.get("schema") != SCHEMA:
            raise ModError("The recovery records use an unsupported format.")
        atomic_json(store.state_path, value)
