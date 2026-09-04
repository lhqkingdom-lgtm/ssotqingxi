[CmdletBinding()]
param(
    [int]$Port = 8765,
    [string]$Bind = '127.0.0.1',
    [string]$WorkspaceRoot = $PSScriptRoot,
    [string]$Template = (Join-Path $PSScriptRoot 'template\ssot_template.xlsx'),
    [string]$InputDir = (Join-Path $PSScriptRoot 'datasheet'),
    [string]$PythonExe,
    [string]$NodeExe
)

$ErrorActionPreference = 'Stop'
$runtimeRoot = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies'
if (-not $PythonExe) {
    $bundledPython = Join-Path $runtimeRoot 'python\python.exe'
    if (Test-Path -LiteralPath $bundledPython) { $PythonExe = $bundledPython }
    else {
        $systemPython = Get-Command python -ErrorAction SilentlyContinue
        if ($systemPython) { $PythonExe = $systemPython.Source }
    }
}
if (-not $NodeExe) {
    $bundledNode = Join-Path $runtimeRoot 'node\bin\node.exe'
    if (Test-Path -LiteralPath $bundledNode) { $NodeExe = $bundledNode }
    else {
        $systemNode = Get-Command node -ErrorAction SilentlyContinue
        if ($systemNode) { $NodeExe = $systemNode.Source }
    }
}
$schema = Join-Path $PSScriptRoot 'config\schema.json'
$server = Join-Path $PSScriptRoot 'src\review_app\review_server.py'

foreach ($required in @($WorkspaceRoot, $Template, $InputDir, $PythonExe, $NodeExe, $schema, $server)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required path not found: $required" }
}

Write-Host "Review service: http://${Bind}:$Port/"
& $PythonExe $server `
    --root $WorkspaceRoot `
    --template $Template `
    --schema $schema `
    --node-exe $NodeExe `
    --input-dir $InputDir `
    --bind $Bind `
    --port $Port
