# Installs the Claude MCP bridge for Maya and the MCP server's Python environment.
#  1. Creates .venv and installs the MCP SDK
#  2. Writes a Maya module file (Documents\maya\modules\claude_mcp.mod) so every
#     Maya version finds the plug-in in .\maya\plug-ins
#  3. Registers the "maya" MCP server with Claude Code (user scope), if `claude` is on PATH
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    python -m venv "$root\.venv"
}
& "$root\.venv\Scripts\python.exe" -m pip install -q -r "$root\requirements.txt"

$modDir = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "maya\modules"
New-Item -ItemType Directory -Force $modDir | Out-Null
$modFile = Join-Path $modDir "claude_mcp.mod"
"+ ClaudeMCP 1.0.0 $root\maya" | Set-Content -Encoding ascii $modFile
Write-Host "Wrote $modFile"

if (Get-Command claude -ErrorAction SilentlyContinue) {
    claude mcp add maya --scope user -- "$root\.venv\Scripts\python.exe" "$root\server\maya_mcp_server.py"
} else {
    Write-Host "Claude Code CLI not on PATH; use .mcp.json in this folder or add the server manually."
}
Write-Host "Done. In Maya: Windows > Settings/Preferences > Plug-in Manager > load claude_mcp_bridge.py (tick Auto load)"
