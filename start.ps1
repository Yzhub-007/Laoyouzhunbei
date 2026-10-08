param(
    [string]$PythonPath = '',
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskCandidates = @()
$taskVenv = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $taskVenv) { $taskCandidates += ,@($taskVenv) }
if ($PythonPath) { $taskCandidates += ,@($PythonPath) }
if (Get-Command py -ErrorAction SilentlyContinue) { $taskCandidates += ,@('py', '-3') }
if (Get-Command python -ErrorAction SilentlyContinue) { $taskCandidates += ,@('python') }
$taskBundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $taskBundled) { $taskCandidates += ,@($taskBundled) }
$taskPython = $null
foreach ($taskCandidate in $taskCandidates) {
    $taskExecutable = $taskCandidate[0]
    $taskPrefix = @($taskCandidate | Select-Object -Skip 1)
    try {
        & $taskExecutable @taskPrefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $taskPython = $taskCandidate; break }
    } catch { }
}
if ($null -eq $taskPython) { throw '需要 Python 3.11 或更新版本。请安装 Python 并先运行 setup.ps1，或用 -PythonPath 指定解释器。' }
$taskExecutable = $taskPython[0]
$taskPrefix = @($taskPython | Select-Object -Skip 1)
& $taskExecutable @taskPrefix -X utf8 (Join-Path $PSScriptRoot 'doctor.py') --check-startup --port $Port
if ($LASTEXITCODE -ne 0) { throw '启动检查未通过，请按上面的说明处理。详细步骤：docs/20_安装与交付.md。' }
if ($CheckOnly) { exit 0 }
Write-Host ('本机访问：http://127.0.0.1:' + $Port + '/；按 Ctrl+C 停止。')
& $taskExecutable @taskPrefix -X utf8 (Join-Path $PSScriptRoot 'server.py') --port $Port
exit $LASTEXITCODE
