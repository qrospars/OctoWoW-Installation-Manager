# Game Mods

Game Mods follows the Addon Manager layout with searchable mod cards, version
status, contextual updates and the same red trash icon. Removal requires a
confirmation, and each installed mod has an Enabled switch.

The main toolbar contains **Add mod** and **Check updates**. Add mod accepts a
source URL or a local MPQ/ZIP. Each card shows **Options**, plus **Install**,
**Update** or **Repair** only when needed. **Files** expands filenames,
descriptions, sizes and source inside the card and stays expanded across refreshes.
**Update all** appears when multiple checked updates are available. **Tools** holds
inventory, conflict inspection and recovery utilities. Tool reports stay inside
Game Mods with a **Back to mods** button; removal/install approval still uses
confirmation dialogs. Interrupted installations expose a direct recovery button.

Non-HD mods have per-file switches in Files. Their selections survive updates,
repairs and whole-mod off/on cycles. A switched-off file remains owned and
verified, is stored as `.mpq.disabled`, and is removed/restored with its mod.
Switches require WoW to be closed. Enabled dependent mods block disabling required
files, and a partially disabled mod cannot satisfy another mod's dependency.

The dedicated HD source stays grouped. Its `patch-Z` merges tables from several
packs, including Raid Visuals, so renaming one archive is not a reliable feature
switch. The HD publisher implements per-pack behavior through HD Switch instead;
this preview does not install that DLL or offer equivalent runtime behavior.
See https://github.com/fmustafayaman/OctoWoW-HD-Switch#supported-setup.

## Sources

- **Manual MPQ or ZIP:** imports only lettered MPQs; scripts, DLLs and other ZIP
  contents are not installed. Use Options → Update from local files for later manual replacements.
- **GitHub releases:** Add mod → From a source URL accepts a repository URL and discovers
  lettered MPQ release assets. Every asset must expose its SHA-256 digest in the
  GitHub API. Repositories without release assets/digests can be imported manually
  or publish a supported manifest; arbitrary repository contents are not installed.
- **HTTPS manifest:** Add mod → From a source URL accepts a manifest URL using the format below.
- **OctoWoW Standard HD:** a dedicated adapter follows the HD maintainer's
  supported release manifest. It does not mix arbitrary latest Reforged packs,
  install the live-switch DLL, or add optional appearance packs.

Multiple independent sources can be tracked. Source changes require a preview and
approval. File ownership collisions are blocked. A source must not reuse another
mod's identity. Options → Change source explicitly migrates an installed mod's identity to
another provider. New hosts and retired files are shown before application.

```json
{
  "schema": 1,
  "id": "example-water",
  "name": "Example Water",
  "version": "1.0.0",
  "requires": [],
  "requires_dlls": [],
  "requires_laa": false,
  "files": [{
    "dest": "Data/patch-W.mpq",
    "url": "https://publisher.example/patch-W.mpq",
    "sha256": "<64 lowercase hex characters>",
    "size": 123456
  }]
}
```

Publishers should use immutable release URLs. Metadata is limited in size,
destinations are restricted to lettered MPQs, HTTPS redirects are restricted to
approved hosts, and every download is verified before applying. Unsupported
formats, changed HD file sets, missing assets, network errors and checksum
mismatches leave installed files intact and show an unavailable/needs-review
status. Cached metadata is marked as previously checked, never a successful new
update check. File digest changes count as updates even at the same version.

## File ownership and recovery

Records live in each game's `.octowow-mods` directory, independent of the manager
executable. Server/base/numeric patches cannot be managed as custom mods. Unknown
MPQs and incomplete downloads remain untouched unless explicitly imported.
The inventory includes root-folder MPQs, noting their location.

An installation-wide Windows lock covers preparation and file application and
also coordinates client settings and base-game updates. Downloads may run while
playing; application, toggling, removal and recovery require WoW to be closed.
The process check uses the selected game's executable path where accessible and
fails closed if the process cannot be identified.

Every file-changing operation journals the previous registry and replaced files
before changing them. Replaced files alone are retained, not a full game backup.
Recovery handles an interrupted operation; Undo last change restores the latest
completed operation. External file changes block destructive removal/restoration.
Removal restores original files, including their original enabled/disabled state.
Recovery records and verified staging currently remain on disk for manual review;
they are not automatically purged.

Verify performs actual SHA-256 checks rather than trusting timestamps. The
read-only overlap inspector compares classic MPQ hash tables across enabled mods,
excluding expected overlaps inside the same managed bundle. It cannot prove
runtime compatibility, fully inspect unsupported archive variants, or determine
whether an overlapping asset is intentional. It reports unsupported archives.

## Builds and validation

Game Mods is imported directly by the application. There is no startup method
replacement, custom executable archive modification, or special HD startup flag.
An ordinary PyInstaller build is provided by `Build-GameMods.ps1`; see README for
the required third-party payloads and patcher. Source contributions must not
include game packs, payloads, personal configuration or generated executables.
Mod ownership and recovery records remain scoped to the selected installation,
independent of the manager executable.

Run `python -m unittest discover -s tests`. Backend cases cover ownership,
integrity, dependencies, updates, cancellation, recovery and per-file choices.
Windows widget tests additionally exercise the normal application integration,
inline files, switches, removal confirmation, embedded reports and updater lock
coordination, using disposable fixtures. They require the UI dependencies.

Before release, perform manual visual and in-game acceptance checks on supported
clients. Widget and backend checks do not establish compatibility with every
future mod, client or publisher change. The supported HD file set and trusted
hosts are deliberately explicit; unsupported publisher changes require an
adapter update rather than silently changing the installed pack.
