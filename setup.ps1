param(
    [string]$PythonPath = '',
    [string]$OfflineWheelDirectory = ''
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw '本项目的加密存储需要 Windows。' }
$taskCandidates = @()
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
if ($null -eq $taskPython) { throw '未找到 Python 3.11+。请从 python.org 安装 Windows 版，或用 -PythonPath 指定解释器。' }
$taskVenv = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskVenv)) {
    $taskExecutable = $taskPython[0]
    $taskPrefix = @($taskPython | Select-Object -Skip 1)
    & $taskExecutable @taskPrefix -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败。请使用包含 venv 与 pip 的标准 Python 安装。' }
}
$taskPipArgs = @('-X', 'utf8', '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check', '--only-binary=:all:', '-r', (Join-Path $PSScriptRoot 'requirements.txt'))
if ($OfflineWheelDirectory) {
    $taskWheels = (Resolve-Path -LiteralPath $OfflineWheelDirectory).Path
    $taskPipArgs += @('--no-index', '--find-links', $taskWheels)
    Write-Host '使用本机 wheel 目录离线安装依赖。'
} else {
    $taskPipArgs += @('--index-url', 'https://pypi.org/simple')
    Write-Host '将从 PyPI 安装 requirements.txt 中的 Pillow；不会下载视觉模型。'
}
& $taskVenv @taskPipArgs
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败。离线环境请按安装说明准备匹配当前 Python 与 Windows 架构的 wheel。' }
& $taskVenv -X utf8 (Join-Path $PSScriptRoot 'doctor.py') --skip-port
if ($LASTEXITCODE -ne 0) { throw '依赖检查未通过，请参照上方诊断。' }
Write-Host '安装完成。请用日常 Windows 账户双击 start.cmd，或运行 .\start.ps1。'
