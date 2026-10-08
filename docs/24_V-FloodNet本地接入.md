# V-FloodNet 本地接入

更新日期：2026-10-06。本项目通过一个只监听 `127.0.0.1` 的 sidecar 服务接入 V-FloodNet 图像分割模型。主工作台仍使用原有摄像头抓拍、ROI、隐私遮挡、时效检查、重复画面跳过和人工核验流程；图片只在内存中传给本机 sidecar，不保存原图或分割掩码。

## 能力边界

- 当前接入使用上游 `link_efficientb4_model.pth` 图像分割模型，把水体分割像素比例转换为 `water_detected` 线索。
- 不接入上游人体、停牌、MeshTransformer、Detectron2 或水深估计链路；不得把分割结果解释为厘米水深、通行安全或正式预警。
- 默认水体像素阈值为 0.5，最小水体占比为 0.2%。这些是运行参数，不是经过紫金港现场校准的业务阈值。
- 原有 `vision_acceptance.py` 仍是上线前验收入口。必须使用现场留出集评估，不能用仓库演示图代替。

## 版权与权重

上游源码快照位于 `third_party/V-FloodNet/`，对应提交 `60c30fbac9d264d020148780504871556285f455`。上游仓库没有 `LICENSE` 文件，README 写明 “All rights are reserved”。本包不声称获得额外授权，向第三方分发或商业使用前应取得权利人许可。

模型权重未随包提供。请从上游 README 指向的官方 Hugging Face 项目获取 `records.zip`，自行核对来源和适用条款，将 `link_efficientb4_model.pth` 放到：

```text
third_party/V-FloodNet/records/link_efficientb4_model.pth
```

不要使用来历不明的 `.pth` 文件。上游权重采用 Python/PyTorch 序列化，加载恶意文件可能执行代码。

## 安装独立环境

V-FloodNet 与主工作台依赖隔离。主工作台继续使用 `.venv` 与 Python 3.11+；V-FloodNet 使用 `.venv-vfloodnet`，建议 Python 3.11。CPU 安装：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\integrations\v_floodnet\setup.ps1 -TorchMode cpu
```

已正确安装 NVIDIA 驱动且需要 CUDA 12.1 时：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\integrations\v_floodnet\setup.ps1 -TorchMode cu121
```

上游完整水深估计仍建议在其验证过的 Linux/WSL2 环境中单独部署；本接入只运行图像分割模型，不需要 GUI、Detectron2 或 MeshTransformer。

## 联合启动

每次启动前，在当前 PowerShell 会话设置一个临时本机服务密钥。该值同时用于 sidecar 和主工作台进程，不写入发布包：

```powershell
$env:V_FLOODNET_API_KEY = '<至少16字符的随机本机密钥>'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-with-vfloodnet.ps1
```

脚本先加载模型并检查 `http://127.0.0.1:8877/health`，随后以环境变量向主工作台注入：

```text
VISION_ENDPOINT=http://127.0.0.1:8877/v1/chat/completions
VISION_MODEL=v-floodnet-image-segmentation
VISION_API_KEY=<当前会话密钥>
```

主工作台仍访问 `http://127.0.0.1:8765/`。关闭主工作台后，脚本会停止由它启动的 sidecar。

## 摄像头配置与验收

在“监测总览”添加视觉摄像头来源，先使用 `monitor-input` 内的合成或已授权图片。远端抓拍仍必须带可信采集时间头，ROI 和隐私遮挡规则不变。检查顺序：

1. `runtime/vfloodnet-service.log` 显示本机端点已启动，错误日志为空。
2. 工作台状态显示视觉模型已配置。
3. 同一画面不会重复触发模型推理；过期、未来时间、低质量或不可解码图片保持未知。
4. 用 `vision_acceptance.py` 对冻结的开发集和留出集评估，再由现场人员确定是否允许进入影子运行。

## 打包范围

v0.9 发布包包含 sidecar、脚本、测试、文档和上游源码快照，不包含：模型权重、数据集、`.venv-vfloodnet`、运行日志、账户、密钥、真实摄像头资料、上游 `.git` 历史或未拉取的子模块内容。
