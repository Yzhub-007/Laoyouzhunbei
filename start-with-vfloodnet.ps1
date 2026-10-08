param(
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [ValidateRange(1, 65535)][int]$VisionPort = 8877,
    [string]$Checkpoint = '',
    [ValidateSet('auto', 'cpu', 'cuda')][string]$Device = 'auto'
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($Port -eq $VisionPort) { throw '工作台端口与V-FloodNet端口不能相同。' }
$taskVisionPython = Join-Path $PSScriptRoot '.venv-vfloodnet\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskVisionPython)) {
    throw '未找到.venv-vfloodnet。请先运行 integrations\v_floodnet\setup.ps1。'
}
if (-not $Checkpoint) {
    $Checkpoint = Join-Path $PSScriptRoot 'third_party\V-FloodNet\records\link_efficientb4_model.pth'
}
if (-not (Test-Path -LiteralPath $Checkpoint -PathType Leaf)) {
    throw '未找到V-FloodNet权重。请按docs\24_V-FloodNet本地接入.md下载并核验官方权重。'
}
$Checkpoint = (Resolve-Path -LiteralPath $Checkpoint).Path
$taskKey = $env:V_FLOODNET_API_KEY
if (-not $taskKey -or $taskKey.Length -lt 16) {
    throw '请先在当前PowerShell设置至少16字符的V_FLOODNET_API_KEY；不要把密钥写入脚本或压缩包。'
}
$taskRuntime = Join-Path $PSScriptRoot 'runtime'
New-Item -ItemType Directory -Force -Path $taskRuntime | Out-Null
$taskOut = Join-Path $taskRuntime 'vfloodnet-service.log'
$taskErr = Join-Path $taskRuntime 'vfloodnet-service-error.log'
$taskService = Join-Path $PSScriptRoot 'integrations\v_floodnet\service.py'
$taskArguments = @('-X', 'utf8', ('"' + $taskService + '"'), '--checkpoint', ('"' + $Checkpoint + '"'),
                   '--port', $VisionPort, '--device', $Device)
$taskProcess = Start-Process -FilePath $taskVisionPython -ArgumentList $taskArguments -PassThru -WindowStyle Hidden -RedirectStandardOutput $taskOut -RedirectStandardError $taskErr
try {
    $taskReady = $false
    for ($taskAttempt = 0; $taskAttempt -lt 120; $taskAttempt++) {
        if ($taskProcess.HasExited) {
            $taskDetails = if (Test-Path -LiteralPath $taskErr) { Get-Content -LiteralPath $taskErr -Raw } else { '' }
            throw ('V-FloodNet服务启动失败。' + [Environment]::NewLine + $taskDetails)
        }
        try {
            $taskHealth = Invoke-RestMethod -Uri ('http://127.0.0.1:' + $VisionPort + '/health') -TimeoutSec 2
            if ($taskHealth.status -eq 'ok' -and $taskHealth.checkpoint_loaded) { $taskReady = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $taskReady) { throw 'V-FloodNet模型在120秒内未完成加载，请查看runtime日志。' }
    $env:VISION_ENDPOINT = 'http://127.0.0.1:' + $VisionPort + '/v1/chat/completions'
    $env:VISION_API_KEY = $taskKey
    $env:VISION_MODEL = 'v-floodnet-image-segmentation'
    Write-Host ('V-FloodNet已就绪；工作台：http://127.0.0.1:' + $Port + '/')
    & (Join-Path $PSScriptRoot 'start.ps1') -Port $Port
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:VISION_ENDPOINT, Env:VISION_API_KEY, Env:VISION_MODEL -ErrorAction SilentlyContinue
    if ($taskProcess -and -not $taskProcess.HasExited) { Stop-Process -Id $taskProcess.Id -Force }
}
