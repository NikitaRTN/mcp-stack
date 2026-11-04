[CmdletBinding()]
param(
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
if (-not $OutputPath) { $OutputPath = Join-Path $PSScriptRoot 'windows_mcp.exe' }
$source = Join-Path $PSScriptRoot 'windows_mcp.cs'
if (-not (Test-Path -LiteralPath $source)) {
    throw "Windows MCP source not found: $source"
}

$frameworkCandidates = @(
    (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'),
    (Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319')
)
$framework = $frameworkCandidates | Where-Object {
    (Test-Path -LiteralPath (Join-Path $_ 'csc.exe')) -and
    (Test-Path -LiteralPath (Join-Path $_ 'WPF\UIAutomationClient.dll')) -and
    (Test-Path -LiteralPath (Join-Path $_ 'WPF\UIAutomationTypes.dll')) -and
    (Test-Path -LiteralPath (Join-Path $_ 'WPF\WindowsBase.dll'))
} | Select-Object -First 1
if (-not $framework) {
    throw 'A compatible .NET Framework C# compiler and UI Automation reference assemblies were not found.'
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null
$csc = Join-Path $framework 'csc.exe'
$arguments = @(
    '/nologo',
    '/target:exe',
    '/optimize+',
    "/out:$OutputPath",
    '/reference:System.dll',
    '/reference:System.Core.dll',
    '/reference:System.Drawing.dll',
    '/reference:System.Windows.Forms.dll',
    '/reference:System.Web.Extensions.dll',
    "/reference:$(Join-Path $framework 'WPF\WindowsBase.dll')",
    "/reference:$(Join-Path $framework 'WPF\UIAutomationClient.dll')",
    "/reference:$(Join-Path $framework 'WPF\UIAutomationTypes.dll')",
    $source
)
& $csc @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Windows MCP compilation failed with exit code $LASTEXITCODE."
}
Write-Output "Built $OutputPath"