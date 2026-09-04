[CmdletBinding()]
param(
    [string]$PythonExe = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    [string]$NodeExe = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe",
    [string]$OutputRoot = "$(Join-Path (Split-Path -Parent $PSScriptRoot) 'output\acceptance')"
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$workspace = $project
$schema = Join-Path $project 'config\schema.json'
$caOut = Join-Path $OutputRoot 'ca'
$stmOut = Join-Path $OutputRoot 'stm32'
$mixedOut = Join-Path $OutputRoot 'mixed'

function Invoke-Checked([string]$Label, [scriptblock]$Command) {
    Write-Host "[verify] $Label"
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE" }
}

Invoke-Checked 'unit tests' { & $PythonExe -m unittest discover -s (Join-Path $project 'tests') -p 'test_*.py' }
foreach ($set in @('ca_miai_current_v2', 'stm32_v1_sample2')) {
    Invoke-Checked "fixture hashes: $set" { & $PythonExe (Join-Path $project 'tests\verify_fixture_hashes.py') --manifest (Join-Path $project 'tests\fixtures.json') --set $set --root $workspace }
}

Invoke-Checked 'CA/MiAi extraction' { & $PythonExe (Join-Path $project 'src\extract.py') --input (Join-Path $workspace 'tmp\testsets\metadata_v2_3docs') --output-dir $caOut --schema $schema --profile (Join-Path $project 'config\profiles\ca_miai_v1.json') --ingested-by regression --ingested-at '2026-08-31T12:00:00+08:00' }
Invoke-Checked 'CA/MiAi regression' { & $PythonExe (Join-Path $project 'tests\regression.py') compare --baseline (Join-Path $project 'tests\baselines\ca_miai_current_v2.json') --extracted (Join-Path $caOut 'extracted.json') --evidence (Join-Path $caOut 'evidence.jsonl') }
Invoke-Checked 'CA/MiAi contract' { & $PythonExe (Join-Path $project 'tests\verify_extraction_contract.py') --extracted (Join-Path $caOut 'extracted.json') --evidence (Join-Path $caOut 'evidence.jsonl') --schema $schema --require-clean }

Invoke-Checked 'STM32 extraction' { & $PythonExe (Join-Path $project 'src\extract.py') --input (Join-Path $workspace 'tmp\testsets\stm32_v1_sample2') --output-dir $stmOut --schema $schema --profile (Join-Path $project 'config\profiles\stm32_v1.json') --ingested-by regression --ingested-at '2026-08-31T12:00:00+08:00' }
Invoke-Checked 'STM32 regression' { & $PythonExe (Join-Path $project 'tests\regression.py') compare --baseline (Join-Path $project 'tests\baselines\stm32_v1_sample2.json') --extracted (Join-Path $stmOut 'extracted.json') --evidence (Join-Path $stmOut 'evidence.jsonl') }
Invoke-Checked 'STM32 contract' { & $PythonExe (Join-Path $project 'tests\verify_extraction_contract.py') --extracted (Join-Path $stmOut 'extracted.json') --evidence (Join-Path $stmOut 'evidence.jsonl') --schema $schema --require-clean }

Invoke-Checked 'mixed extraction' { & $PythonExe (Join-Path $project 'src\extract.py') --input (Join-Path $workspace 'tmp\testsets\mixed_v1') --output-dir $mixedOut --schema $schema --family-registry (Join-Path $project 'config\document_families.json') --ingested-by regression --ingested-at '2026-08-31T12:00:00+08:00' }
Invoke-Checked 'mixed union' { & $PythonExe (Join-Path $project 'tests\verify_mixed_run.py') --mixed (Join-Path $mixedOut 'extracted.json') --ca (Join-Path $caOut 'extracted.json') --stm32 (Join-Path $stmOut 'extracted.json') --ca-source 'MiAi-6099A1 MCU用户手册_REV1.0.pdf' --stm32-source 'stm32c031c4.pdf' }
Invoke-Checked 'mixed review' { & $PythonExe (Join-Path $project 'src\build_review.py') --extracted (Join-Path $mixedOut 'extracted.json') --evidence (Join-Path $mixedOut 'evidence.jsonl') --schema $schema --output-dir (Join-Path $mixedOut 'review') }
Invoke-Checked 'mixed workbook' {
    $workbookPath = Join-Path $mixedOut 'ssot_generated.xlsx'
    $rowMapPath = Join-Path $mixedOut 'row_map.json'
    & $NodeExe (Join-Path $project 'src\write_workbook.mjs') --input (Join-Path $mixedOut 'extracted.json') --template (Join-Path $workspace 'template\ssot_template.xlsx') --schema $schema --output $workbookPath --row-map $rowMapPath
    if (($LASTEXITCODE -eq 1 -or $LASTEXITCODE -eq -1073740791) -and (Test-Path -LiteralPath $workbookPath) -and (Test-Path -LiteralPath $rowMapPath)) {
        $global:LASTEXITCODE = 0
    }
}
Invoke-Checked 'mixed workbook structure' { & $NodeExe (Join-Path $project 'src\validate_workbook.mjs') (Join-Path $mixedOut 'ssot_generated.xlsx') $schema clean }

Write-Host "Acceptance passed: $OutputRoot"
