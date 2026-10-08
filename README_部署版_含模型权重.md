# 涝有准备 v0.9 + V-FloodNet 部署说明（含模型权重）

更新日期：2026-10-07

本部署包在原始 v0.9 源码交付包基础上加入了 V-FloodNet 图像分割权重：

```text
third_party/V-FloodNet/records/link_efficientb4_model.pth
```

权重文件大小为 `72,294,975` 字节，SHA-256：

```text
ab073b386a30375e0947246dbfcbab5b23197056d8918841f6b7e2764add7440
```

为减少部署包体积，本包不重复包含上游 `records.zip`、数据集、演示结果、虚拟环境、Python 安装程序、账号、密钥或运行数据。V-FloodNet 当前只用于判断画面中是否疑似存在水体，不提供厘米水深、通行安全、设施关闭或正式预警结论。

## 1. 部署要求

- Windows 10/11 64 位。
- 建议安装 Python 3.11 64 位；主工作台与 V-FloodNet 均可使用该版本创建各自的虚拟环境。
- CPU 模式无需 NVIDIA 显卡，部署最简单。
- CUDA 模式需要兼容 CUDA 12.1 的 NVIDIA 显卡和驱动，安装前应由设备管理员确认驱动兼容性。
- CPU 部署建议至少保留 5 GB 可用磁盘空间；CUDA 部署建议至少保留 10—15 GB。
- 项目应解压到本机受控目录，不要直接在 ZIP 内运行，也不要放到所有用户均可写的共享目录。

## 2. 校验部署包

将 ZIP 和同名 `.sha256` 文件放在同一目录后执行：

```powershell
Get-FileHash -LiteralPath '.\涝有准备_完整项目_v0.9_V-FloodNet_含模型权重_完整README_v3.zip' -Algorithm SHA256
Get-Content -LiteralPath '.\涝有准备_完整项目_v0.9_V-FloodNet_含模型权重_完整README_v3.zip.sha256'
```

两个 SHA-256 值必须一致。解压后可查看 `DEPLOYMENT_MANIFEST.json`，其中记录了部署包内各文件的大小和 SHA-256。

可单独核验模型权重：

```powershell
Get-FileHash -LiteralPath '.\third_party\V-FloodNet\records\link_efficientb4_model.pth' -Algorithm SHA256
```

只有在结果为 `ab073b386a30375e0947246dbfcbab5b23197056d8918841f6b7e2764add7440` 时才继续部署。

## 3. 安装主工作台

在今后负责运行系统的同一个日常 Windows 账户下打开 PowerShell，进入解压后的项目目录：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

这会创建 `.venv` 并安装主工作台依赖。正式运行数据使用 Windows DPAPI 绑定当前 Windows 账户，因此不要使用临时沙箱账户初始化正式实例。

## 4. 安装 V-FloodNet 环境

CPU 部署：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\integrations\v_floodnet\setup.ps1 `
  -TorchMode cpu
```

CUDA 12.1 部署：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\integrations\v_floodnet\setup.ps1 `
  -TorchMode cu121
```

安装脚本会创建独立的 `.venv-vfloodnet`，避免 PyTorch 与主工作台依赖相互污染。不要把其他电脑生成的虚拟环境直接复制过来；应在目标电脑重新创建。

## 5. 联合启动

每次启动前，在当前 PowerShell 会话生成临时本机服务密钥：

```powershell
$bytes = [byte[]]::new(32)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$env:V_FLOODNET_API_KEY = [Convert]::ToHexString($bytes)
```

CPU 启动：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\start-with-vfloodnet.ps1 `
  -Device cpu
```

CUDA 启动：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\start-with-vfloodnet.ps1 `
  -Device cuda
```

脚本会先加载模型并检查本机视觉服务，然后启动工作台：

```text
工作台：http://127.0.0.1:8765/
视觉服务：http://127.0.0.1:8877/v1/chat/completions
健康检查：http://127.0.0.1:8877/health
```

关闭工作台后，联合启动脚本会停止由它启动的视觉服务。

## 6. 首次管理员初始化

首次启动会生成有效期为 20 分钟的设置码：

```powershell
Get-Content -LiteralPath .\runtime\setup-code.txt
```

在浏览器中输入该码并创建管理员。密码至少 15 个字符。初始化完成后设置码文件会被删除。

## 7. 安装后检查

主工作台自检：

```powershell
& .\.venv\Scripts\python.exe -X utf8 .\doctor.py --json --skip-port
```

后端测试：

```powershell
& .\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p 'test_*.py' -v
```

联合启动期间检查 V-FloodNet：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8877/health'
Get-Content -LiteralPath .\runtime\vfloodnet-service.log
Get-Content -LiteralPath .\runtime\vfloodnet-service-error.log
```

健康检查应返回 `status: ok` 和 `checkpoint_loaded: true`。首次 CPU 加载模型可能需要较长时间。

## 8. 摄像头与现场数据对接

- RTSP/RTSPS 网络直连当前停用，应由本机或校内 HTTPS 网关提供 JPEG/PNG 抓拍。
- 抓拍响应必须提供真实采集时间，例如带时区的 `X-Captured-At`，或可信的 `Last-Modified`。
- 校内远程来源需要登记精确的 `CAMERA_TRUSTED_ORIGINS`；远程视觉服务还必须配置非全画面 ROI。
- ROI、隐私遮挡、点位 ID、有效期和连续确认次数必须根据现场视野配置。
- 当前紫金港点位是待确认模板，不能当作已经核实的校园风险点。
- 上线前必须使用现场开发集和独立留出集运行 `vision_acceptance.py`，不能使用仓库演示图片代替验收。

## 9. 离线部署到其他电脑

在联网且与目标机器架构一致的 Windows 电脑上，使用 Python 3.11 下载 wheel：

```powershell
New-Item -ItemType Directory -Force -Path .\wheels | Out-Null
python -m pip download --only-binary=:all: --dest .\wheels -r .\requirements.txt
python -m pip download --dest .\wheels `
  --index-url https://download.pytorch.org/whl/cpu `
  torch==2.2.2 torchvision==0.17.2
python -m pip download --dest .\wheels `
  -r .\integrations\v_floodnet\requirements.txt
```

将项目、`wheels` 和 Python 3.11 64 位安装程序复制到目标电脑，然后执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\setup.ps1 `
  -OfflineWheelDirectory 'D:\部署介质\wheels'

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\integrations\v_floodnet\setup.ps1 `
  -TorchMode cpu `
  -OfflineWheelDirectory 'D:\部署介质\wheels'
```

CUDA 离线部署应准备与目标显卡和驱动匹配的 CUDA 12.1 wheel。不要混用 CPU 与 CUDA 的 PyTorch wheel。

## 10. 数据迁移与安全边界

- `.venv` 和 `.venv-vfloodnet` 不应跨电脑复制，目标电脑应重新安装。
- `runtime` 中的数据库、账号和密钥使用 Windows DPAPI CurrentUser 加密；直接复制到其他电脑或其他 Windows 账户通常无法解密。
- 新电脑部署应重新创建管理员、密钥和监测配置。历史业务数据迁移需要单独设计并验证导出、导入和恢复流程。
- 不要把 `V_FLOODNET_API_KEY`、摄像头凭据或其他密钥写入 README、脚本、ZIP 或聊天记录。
- 服务仅监听本机回环地址，不能直接作为公网或普通局域网多用户服务发布。
- 导出的 HTML/JSON 是可读文件，应按内部资料管理。

## 11. 权重授权说明

V-FloodNet 上游源码快照对应提交 `60c30fbac9d264d020148780504871556285f455`。上游仓库没有开源许可证文件，并声明 “All rights are reserved”。本部署包加入模型权重仅用于经授权的本地试验和复现，不代表已取得对外再分发、公开发布或商业使用许可。

在把本部署包提供给第三方之前，应由项目负责人确认源码和模型权重的授权范围。若无法确认，应交付不含模型权重的原始源码包，并由使用方自行从官方来源取得权重。

## 12. 当前能力边界

- 模型只提供疑似水体线索，不提供厘米水深。
- 不自动判定道路、入口或设施是否安全。
- 不自动发布预警、短信、企业微信、广播或设施关闭指令。
- 模型默认阈值尚未经过紫金港现场标定。
- 天气模型和机场观测不能代替校园点位实测或官方预警。
- 所有自动结果必须保留人工核验、来源时间和责任人记录。
