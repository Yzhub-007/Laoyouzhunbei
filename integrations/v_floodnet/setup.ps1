param(
    [string]$PythonPath = '',
    [ValidateSet('cpu', 'cu121', 'skip')][string]$TorchMode = 'cpu',
    [string]$OfflineWheelDirectory = ''
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskCandidates = @()
if ($PythonPath) { $taskCandidates += ,@($PythonPath) }
if (Get-Command py -ErrorAction SilentlyContinue) { $taskCandidates += ,@('py', '-3.11') }
if (Get-Command python -ErrorAction SilentlyContinue) { $taskCandidates += ,@('python') }
$taskPython = $null
foreach ($taskCandidate in $taskCandidates) {
    $taskExecutable = $taskCandidate[0]
    $taskPrefix = @($taskCandidate | Select-Object -Skip 1)
    try {
        & $taskExecutable @taskPrefix -c "import sys; raise SystemExit(0 if (3,8) <= sys.version_info[:2] <= (3,11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $taskPython = $taskCandidate; break }
    } catch { }
}
if ($null -eq $taskPython) { throw '需要独立的Python 3.8—3.11环境；上游V-FloodNet未验证Python 3.12+。' }
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$taskVenv = Join-Path $taskRoot '.venv-vfloodnet\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskVenv)) {
    $taskExecutable = $taskPython[0]
    $taskPrefix = @($taskPython | Select-Object -Skip 1)
    & $taskExecutable @taskPrefix -m venv (Join-Path $taskRoot '.venv-vfloodnet')
    if ($LASTEXITCODE -ne 0) { throw '创建V-FloodNet虚拟环境失败。' }
}
$taskCommon = @('-X', 'utf8', '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check')
if ($OfflineWheelDirectory) {
    $taskWheels = (Resolve-Path -LiteralPath $OfflineWheelDirectory).Path
    $taskCommon += @('--no-index', '--find-links', $taskWheels)
}
if ($TorchMode -ne 'skip') {
    $taskTorch = @('torch==2.2.2', 'torchvision==0.17.2')
    $taskArgs = @($taskCommon) + $taskTorch
    if (-not $OfflineWheelDirectory) {
        $taskIndex = if ($TorchMode -eq 'cu121') { 'https://download.pytorch.org/whl/cu121' } else { 'https://download.pytorch.org/whl/cpu' }
        $taskArgs += @('--index-url', $taskIndex)
    }
    & $taskVenv @taskArgs
    if ($LASTEXITCODE -ne 0) { throw 'PyTorch安装失败；请确认Python版本、显卡/CUDA模式或离线wheel。' }
}
$taskArgs = @($taskCommon) + @('-r', (Join-Path $PSScriptRoot 'requirements.txt'))
if (-not $OfflineWheelDirectory) { $taskArgs += @('--index-url', 'https://pypi.org/simple') }
& $taskVenv @taskArgs
if ($LASTEXITCODE -ne 0) { throw 'V-FloodNet Python依赖安装失败。' }
& $taskVenv -c "import torch, torchvision, segmentation_models_pytorch, PIL; print('V-FloodNet runtime OK; torch=' + torch.__version__)"
if ($LASTEXITCODE -ne 0) { throw 'V-FloodNet运行环境自检失败。' }
Write-Host 'V-FloodNet环境安装完成。请按docs/24_V-FloodNet本地接入.md放置官方权重。'
