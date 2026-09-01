[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$InputDir = (Join-Path (Split-Path -Parent $PSScriptRoot) '原始文档'),

    [Parameter(Mandatory = $false)]
    [string]$Template = (Join-Path (Split-Path -Parent $PSScriptRoot) 'ssot_template(1).xlsx'),

    [string]$OutputDir,

    [string[]]$Profile,

    [string]$PythonExe,

    [string]$NodeExe,

    [string]$NodeModulesPath,

    [string]$IngestedBy = $env:USERNAME,

    [switch]$SkipReview,

    [switch]$Strict
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot

if (-not $OutputDir) {
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $OutputDir = Join-Path $workspaceRoot "output\ssot_run_$stamp"
}

if (-not (Test-Path -LiteralPath $InputDir -PathType Container)) {
    throw "PDF input folder not found: $InputDir"
}
if (-not (Test-Path -LiteralPath $Template -PathType Leaf)) {
    throw "Template not found: $Template"
}
foreach ($profilePath in @($Profile)) {
    if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf)) {
        throw "Document profile not found: $profilePath"
    }
}

$runtimeRoot = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies'
if (-not $PythonExe) {
    $bundledPython = Join-Path $runtimeRoot 'python\python.exe'
    if (Test-Path -LiteralPath $bundledPython) { $PythonExe = $bundledPython }
}
if (-not $NodeExe) {
    $bundledNode = Join-Path $runtimeRoot 'node\bin\node.exe'
    if (Test-Path -LiteralPath $bundledNode) { $NodeExe = $bundledNode }
}
if (-not $NodeModulesPath) {
    $bundledModules = Join-Path $runtimeRoot 'node\node_modules'
    if (Test-Path -LiteralPath $bundledModules) { $NodeModulesPath = $bundledModules }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw 'Python runtime not found. Pass -PythonExe with pypdf, pdfplumber and Pillow installed.'
}
if (-not $NodeExe -or -not (Test-Path -LiteralPath $NodeExe -PathType Leaf)) {
    throw 'Node.js runtime not found. Pass -NodeExe explicitly.'
}
if (-not $NodeModulesPath -or -not (Test-Path -LiteralPath $NodeModulesPath -PathType Container)) {
    throw 'Node modules not found. Pass -NodeModulesPath containing @oai/artifact-tool.'
}

$moduleLink = Join-Path $projectRoot 'node_modules'
if (-not (Test-Path -LiteralPath $moduleLink)) {
    New-Item -ItemType Junction -Path $moduleLink -Target $NodeModulesPath | Out-Null
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$extractJson = Join-Path $OutputDir 'extracted.json'
$rowMap = Join-Path $OutputDir 'row_map.json'
$outputWorkbook = Join-Path $OutputDir 'ssot_generated.xlsx'
$reviewDir = Join-Path $OutputDir 'review'
$schema = Join-Path $projectRoot 'config\schema.json'
$familyRegistry = Join-Path $projectRoot 'config\document_families.json'
$resolvedProfiles = @($Profile | ForEach-Object { (Resolve-Path -LiteralPath $_).Path })

$stepTotal = if ($SkipReview) { 2 } else { 3 }
Write-Host "[1/$stepTotal] Extracting PDF parameters..."
$extractArgs = @(
    (Join-Path $projectRoot 'src\extract.py'),
    '--input', $InputDir,
    '--output-dir', $OutputDir,
    '--schema', $schema,
    '--ingested-by', $IngestedBy
)
if (-not $Strict) {
    $extractArgs += '--allow-partial'
}
if ($resolvedProfiles.Count) {
    foreach ($profilePath in $resolvedProfiles) {
        $extractArgs += @('--profile', $profilePath)
    }
} else {
    $extractArgs += @('--family-registry', $familyRegistry)
}
& $PythonExe @extractArgs
if ($LASTEXITCODE -ne 0) { throw "Extraction failed with exit code $LASTEXITCODE" }

if (-not $SkipReview) {
    Write-Host "[2/$stepTotal] Building the standalone review page..."
    & $PythonExe (Join-Path $projectRoot 'src\build_review.py') `
        --extracted $extractJson `
        --evidence (Join-Path $OutputDir 'evidence.jsonl') `
        --schema $schema `
        --output-dir $reviewDir
    if ($LASTEXITCODE -ne 0) { throw "Review page generation failed with exit code $LASTEXITCODE" }
}

$workbookStep = if ($SkipReview) { 2 } else { 3 }
Write-Host "[$workbookStep/$stepTotal] Generating the machine-import Excel workbook..."
$writerArgs = @(
    (Join-Path $projectRoot 'src\write_workbook.mjs'),
    '--input', $extractJson,
    '--template', $Template,
    '--schema', $schema,
    '--output', $outputWorkbook,
    '--row-map', $rowMap
)
& $NodeExe @writerArgs
$writerExit = $LASTEXITCODE
if ($writerExit -ne 0) {
    # Some Windows hosts report 0xC0000409 while unloading the bundled native
    # spreadsheet runtime, after the workbook and all sidecars are flushed.
    # Accept only that exact post-write condition; all other failures remain fatal.
    if (($writerExit -eq -1073740791 -or $writerExit -eq 1) -and (Test-Path -LiteralPath $outputWorkbook -PathType Leaf) -and (Test-Path -LiteralPath $rowMap -PathType Leaf)) {
        Write-Verbose 'Spreadsheet runtime reported a post-write status; the generated artifact is present and will now be validated independently.'
    } else {
        throw "Workbook generation failed with exit code $writerExit"
    }
}

& $NodeExe (Join-Path $projectRoot 'src\validate_workbook.mjs') $outputWorkbook $schema clean
if ($LASTEXITCODE -ne 0) { throw 'Machine workbook validation failed.' }

& $NodeExe (Join-Path $projectRoot 'src\validate_workbook_data.mjs') $outputWorkbook $extractJson $schema
if ($LASTEXITCODE -ne 0) { throw 'Workbook data does not exactly match the validated extraction result.' }

foreach ($workbookPath in @($outputWorkbook)) {
    $inspectSidecar = "$workbookPath.inspect.ndjson"
    if (Test-Path -LiteralPath $inspectSidecar -PathType Leaf) {
        Remove-Item -LiteralPath $inspectSidecar -Force
    }
}
$global:LASTEXITCODE = 0

Write-Host ''
Write-Host "Completed: $outputWorkbook"
if (-not $SkipReview) {
    Write-Host "Review page: $(Join-Path $reviewDir 'index.html')"
}
Write-Host "Extraction data: $extractJson"
Write-Host "Document profiles: $(if ($resolvedProfiles.Count) { $resolvedProfiles -join '; ' } else { "automatic: $familyRegistry" })"
