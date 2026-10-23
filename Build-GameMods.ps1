[CmdletBinding()]
param([string]$Python = "python")

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    foreach ($required in @('Payload', 'vanilla-tweaks.exe', 'PurpleWowLogo.ico')) {
        if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $required))) {
            throw "Missing $required. Supply the original project's documented build inputs first."
        }
    }
    & $Python -m PyInstaller --noconfirm --noconsole --onefile --name OctoWow_Install_Manager `
        --icon PurpleWowLogo.ico --paths . --hidden-import customtkinter `
        --collect-all customtkinter --collect-all tkinterdnd2 `
        --hidden-import game_mods --hidden-import game_mods_ui --hidden-import hd_source `
        --hidden-import mpq_inspect --hidden-import ctypes.wintypes `
        --add-data 'Payload;Payload' --add-data 'vanilla-tweaks.exe;.' `
        --add-data 'PurpleWowLogo.ico;.' OctoWow_Install_Manager.py
    if ($LASTEXITCODE -ne 0) { throw 'The manager build failed.' }
}
finally {
    Pop-Location
}
