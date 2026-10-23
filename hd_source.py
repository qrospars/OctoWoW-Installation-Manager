"""Adapter for the maintainer's explicitly supported Standard OctoWoW set."""

import ctypes
from pathlib import Path

import game_mods

REPO = "fmustafayaman/OctoWoW-HD-Switch"
HOSTS = game_mods.GITHUB_HOSTS | {
    "pub-a854abaa40ad406a86b1fc6390dd259c.r2.dev",
    "pub-0f05631d243e4046993fc02ca7be9542.r2.dev",
}
STANDARD = {"data/patch-" + x.lower() + ".mpq" for x in "ABCDEGIMPSTOFHZ"}


def validate_manifest(manifest):
    if not isinstance(manifest.get("release"), str) or len(manifest["release"]) > 100:
        raise game_mods.ModError("Unsupported HD release metadata.")
    components = [c for c in manifest["components"] if c["id"] == "packs"]
    if len(components) != 1:
        raise game_mods.ModError("HD source no longer contains one Standard pack set.")
    package = game_mods.validate_package(
        {
            "id": "hd-standard",
            "name": "Standard HD",
            "version": manifest["release"],
            "files": components[0]["files"],
        }
    )
    if {i["dest"].lower() for i in package["files"]} != STANDARD:
        raise game_mods.ModError(
            "The supported HD file set changed. Keep the installed set and update/review the source adapter first."
        )
    for item in package["files"]:
        if game_mods.host(item["url"]) not in HOSTS:
            raise game_mods.ModError(
                "The HD publisher changed download hosts. Review the new source before updating."
            )
    return {"files": package["files"]}


def latest_release():
    release = game_mods.json_url("https://api.github.com/repos/" + REPO + "/releases/latest", HOSTS)
    assets = [a for a in release.get("assets", []) if a["name"] == "manifest-v3.json"]
    if len(assets) != 1:
        raise game_mods.ModError(
            "The HD publisher changed its manifest format. Existing mods remain installed."
        )
    manifest = game_mods.json_url(assets[0]["browser_download_url"], HOSTS)
    if manifest["release"] != release["tag_name"]:
        raise game_mods.ModError("HD release and manifest versions do not match.")
    validate_manifest(manifest)
    return manifest


def game_running(root=None):
    """Check this installation by executable path; fail closed on query errors."""
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("pid", wintypes.DWORD),
            ("heap", ctypes.c_size_t),
            ("module", wintypes.DWORD),
            ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD),
            ("priority", wintypes.LONG),
            ("flags", wintypes.DWORD),
            ("name", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise game_mods.ModError("Cannot check running processes; game files were not changed.")
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(handle, ctypes.byref(entry))
        if not found:
            raise game_mods.ModError("Cannot enumerate running processes.")
        while found:
            if entry.name.lower() in {
                "wow.exe",
                "wow_tweaked.exe",
                "wow_modernized.exe",
                "vanillafixes.exe",
            }:
                if root is None:
                    return True
                process = kernel.OpenProcess(0x1000, False, entry.pid)
                if not process:
                    return True
                try:
                    name = ctypes.create_unicode_buffer(32768)
                    length = wintypes.DWORD(len(name))
                    if not kernel.QueryFullProcessImageNameW(
                        process, 0, name, ctypes.byref(length)
                    ):
                        return True
                    if Path(name.value).resolve().parent == Path(root).resolve():
                        return True
                finally:
                    kernel.CloseHandle(process)
            found = kernel.Process32NextW(handle, ctypes.byref(entry))
        return False
    finally:
        kernel.CloseHandle(handle)
