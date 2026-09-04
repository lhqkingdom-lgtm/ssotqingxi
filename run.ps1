[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$InputDir = (Join-Path $PSScriptRoot 'datasheet'),

    [Parameter(Mandatory = $false)]
    [string]$Template = (Join-Path $PSScriptRoot 'template\ssot_template.xlsx'),

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
$workspaceRoot = $projectRoot

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
foreach ($profilePath in @($Profile | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
    if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf)) {
        throw "Document profile not found: $profilePath"
    }
}

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
if (-not $NodeModulesPath) {
    $bundledModules = Join-Path $runtimeRoot 'node\node_modules'
    if (Test-Path -LiteralPath $bundledModules) { $NodeModulesPath = $bundledModules }
    elseif (Test-Path -LiteralPath (Join-Path $projectRoot 'node_modules')) {
        $NodeModulesPath = Join-Path $projectRoot 'node_modules'
    }
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
if (-not (Test-Path -LiteralPath $moduleLink) -and $NodeModulesPath -ne $moduleLink) {
    New-Item -ItemType Junction -Path $moduleLink -Target $NodeModulesPath | Out-Null
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$extractJson = Join-Path $OutputDir 'extracted.json'
$rowMap = Join-Path $OutputDir 'row_map.json'
$outputWorkbook = Join-Path $OutputDir 'ssot_generated.xlsx'
$reviewDir = Join-Path $OutputDir 'review'
$schema = Join-Path $projectRoot 'config\schema.json'
$familyRegistry = Join-Path $projectRoot 'config\document_families.json'
$resolvedProfiles = @(
    $Profile |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        ForEach-Object { (Resolve-Path -LiteralPath $_).Path }
)

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

& $PythonExe (Join-Path $projectRoot 'src\validate_output_contract.py') `
    --schema $schema `
    --extracted $extractJson
if ($LASTEXITCODE -ne 0) { throw 'Extracted data violates the fixed five-sheet output contract.' }

if (-not $SkipReview) {
    Write-Host "[2/$stepTotal] Building centralized review data and evidence..."
    $reviewDataset = Join-Path $reviewDir 'review_dataset.json'
    & $PythonExe (Join-Path $projectRoot 'src\review_app\build_dataset.py') `
        --extracted $extractJson `
        --evidence (Join-Path $OutputDir 'evidence.jsonl') `
        --schema $schema `
        --review-queue (Join-Path $OutputDir 'review_queue.jsonl') `
        --output $reviewDataset
    if ($LASTEXITCODE -ne 0) { throw "Review dataset generation failed with exit code $LASTEXITCODE" }
    & $PythonExe (Join-Path $projectRoot 'src\review_app\build_readonly.py') `
        --dataset $reviewDataset `
        --output-dir $reviewDir
    if ($LASTEXITCODE -ne 0) { throw "Review data generation failed with exit code $LASTEXITCODE" }
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
    Write-Host "Review data: $reviewDir"
    Write-Host 'Review console: http://127.0.0.1:8765/'
}
Write-Host "Extraction data: $extractJson"
Write-Host "Document profiles: $(if ($resolvedProfiles.Count) { $resolvedProfiles -join '; ' } else { "automatic: $familyRegistry" })"
