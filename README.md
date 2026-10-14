# 涝有准备：防汛行动工作台 v0.9 + V-FloodNet

更新日期：2026-10-07

“涝有准备”是一个面向校园、楼宇和小范围管理场景的本机防汛辅助工作台。系统把气象参考、摄像头积水线索、结构化水深观测、人工核验、行动清单和应急预案草案放在同一个可审计流程中，帮助管理人员回答以下问题：

- 当前有哪些来源仍然有效？
- 哪些点位存在疑似积水或水深异常？
- 哪些信息仍然未知，不能被解释为安全？
- 根据已登记的预案条目，当前应由谁核验什么？
- 一份预案草案使用了哪些证据、由谁保存和复核？

本项目不是自动预警系统，也不代替正式应急指挥、校园管理制度或现场人员判断。摄像头模型只产生“疑似有水”的线索；系统不会仅凭模型结果自动关闭设施、判断道路可通行、发布预警或向公众推送信息。

当前版本以浙江大学紫金港校区试点模板为示例。包内默认楼宇、入口、通道和设施仍是待管理方确认的采集槽位，不代表已经核实的校园风险点。

## 系统解决的问题与核心输出

本系统解决的核心问题是：**把分散、时效不同、可信度不同的防汛信息，整理成一份有依据、可追踪、待人工确认的行动清单。**

它不是自动防汛指挥系统，也不会仅凭天气或模型结果自行封路、关闭入口或发布预警。系统的主要价值在于连接“现场证据”和“已经登记的管理预案”，帮助管理人员知道当前应该核验什么、由谁处理、依据是什么以及是否已经完成。

整体工作链路如下：

```text
气象参考 + 摄像头 + 水深设备 + 人工核验
                    ↓
判断数据是否新鲜、可信、冲突或未知
                    ↓
形成各点位当前状态
                    ↓
匹配已经登记的正式预案条目
                    ↓
生成按点位、责任角色划分的行动清单
                    ↓
人工确认、执行、记录、复核和导出预案草案
```

### 解决的问题一：防汛信息分散

防汛过程中可能同时存在天气预报、官方预警、摄像头画面、水深设备、现场人员反馈以及不同楼宇的管理要求。系统把这些信息统一关联到具体点位，例如：

```text
点位：ZJG-P01 地下入口
摄像头：疑似有水
水深设备：未接入
观测时间：5分钟前
综合状态：疑似积水，待人工核验
```

每项信息都保留来源、观测时间、接收时间、有效期和质量状态，避免不同来源之间失去对应关系。

### 解决的问题二：旧数据或缺失数据被误认为安全

系统会检查：

- 图片和传感器数据是否过期。
- 设备时间是否异常或来自未来。
- 摄像头是否持续返回同一张旧图片。
- 数据源是否断流。
- 画质和模型置信度是否足够。
- 摄像头、水深设备和人工反馈是否相互冲突。

没有可靠数据时，系统显示“未知”，不会用零值、旧记录或天气模型推断现场安全。

### 解决的问题三：监测结果无法转化为具体任务

系统会把点位状态与已经登记的预案条目匹配，生成待人工确认的防汛行动清单。例如：

| 点位 | 当前线索 | 建议行动 | 责任角色 | 状态 |
| --- | --- | --- | --- | --- |
| 地下入口 A | 摄像头疑似有水 | 核对入口现场状态和现行预案 | 楼宇管理人员 | 待核验 |
| 主入口 B | 观测已过期 | 检查摄像头和抓拍网关 | 设备维护人员 | 待处理 |
| 通道 C | 水深达到关注阈值 | 现场复核水深及通行状态 | 片区管理人员 | 待核验 |
| 信息联络点 | 存在已确认异常点位 | 准备内部信息草稿 | 信息联络人员 | 待审核 |

每条行动会关联：

- 对应的点位和监测来源。
- 观测时间和状态依据。
- 使用的预案条目。
- 责任角色和复核角色。
- 确认人、确认依据和处理状态。

模型不能凭空创造处置措施。只有先将正式预案、点位和责任角色录入系统，才能生成有管理依据的行动清单。

### 解决的问题四：防汛过程缺少记录和复盘依据

系统能够保存行动清单、人工确认、状态变更、人工校正、监测事件、预案版本和内部复核记录。因此可以追溯：

- 当时有哪些有效信息。
- 为什么产生某项任务。
- 哪个来源支持该判断。
- 谁进行了确认或修改。
- 后续状态如何变化。

### 系统可以输出什么

系统主要产生以下结果：

1. **监测总览**：展示每个点位是未知、未检出、疑似积水、水深异常或达到关注阈值。
2. **防汛行动清单**：按点位、任务和责任角色生成待办事项。
3. **应急预案草案**：把点位、证据、任务、角色和注意事项整理为版本化文档。
4. **公众提示草稿**：根据已经确认的信息生成待审核文字，但不自动发布。
5. **设备和数据异常清单**：提示断流、旧图片、低质量数据和冲突来源。
6. **审计与复盘记录**：保存信息来源、人工处理过程和版本变化。

### 是否能够生成整体防汛行动清单

系统具备生成场地级或校区级综合行动清单的能力，但完整程度取决于已经录入的数据范围。如果已经补齐全部管理点位、监测来源、正式预案、责任角色和触发规则，系统可以将当前有效证据转换为整体行动清单。

如果数据只覆盖一个楼宇，生成的是该楼宇范围的清单；如果完整覆盖整个校区，才可能形成校区级清单。系统不会为未建档的区域、未登记的责任部门或不存在的预案自动编造任务。

当前紫金港资料仍是 6 个待确认点位模板，预案也主要是团队草稿，因此当前输出属于流程演练或待核验行动草案，不是浙江大学正式防汛行动清单。

一句话概括本系统的定位：

> 持续整理现场证据，识别哪些点位需要关注，并把正式预案转换成有责任人、有依据、有状态、有记录的防汛待办清单。

## 1. 当前交付内容

当前“含模型权重部署版”包括：

- 本机 Web 工作台前端与 Python 后端。
- 本地账户、角色权限、会话和 CSRF 防护。
- 试点场地、点位和预案条目建档。
- 自动监测、人工校正和证据时效判断。
- 气象模型与机场报文参考。
- 摄像头安全抓拍、ROI、隐私遮挡和重复画面跳过。
- V-FloodNet 本地图片分割 sidecar。
- 预案草案生成、不可覆盖版本、内部复核和 HTML/JSON 导出。
- 试验计时、观察表模板和离线视觉验收工具。
- Windows DPAPI 加密存储、实例锁、诊断、测试和发布清单。
- V-FloodNet 图像分割权重：

```text
third_party/V-FloodNet/records/link_efficientb4_model.pth
```

模型权重 SHA-256：

```text
ab073b386a30375e0947246dbfcbab5b23197056d8918841f6b7e2764add7440
```

本包不包含 Python 安装程序、虚拟环境、正式账号、密钥、真实摄像头资料、真实校园点位数据或现场验收结论。

快速部署可直接阅读 [README_部署版_含模型权重.md](README_部署版_含模型权重.md)。更细的专题说明位于 `docs/`。

## 2. 设计原则

### 2.1 未知不是安全

缺少观测、来源过期、设备断线、图片无法解码、画质差、模型未配置或时间异常时，系统保持“未知”，不会用零值或旧记录填补，也不会把“未检测到”扩大解释为“现场安全”。

### 2.2 证据与结论分离

天气模型、机场观测、摄像头、水深设备和人工校正分别保存来源、观测时间、接收时间和有效期。系统展示每项结论的证据链，但管理结论仍需有权限的人员核验。

### 2.3 模型只做有限选择

规则模式只从已登记的预案条目中匹配任务。可选文字模型同样只能在候选的规范化任务中进行选择，不能创造未登记的处置指令，也不能修改点位事实。

### 2.4 人工复核优先

模型与监测服务生成的任务状态为 `pending_review`。人工校正必须记录理由和有效期；出现更高关注程度的新证据时，系统会结束可能过度乐观的人工校正。

### 2.5 本机最小暴露

工作台和 V-FloodNet sidecar 默认只监听 `127.0.0.1`。密钥和数据库使用当前 Windows 账户的 DPAPI 加密，不提供明文降级模式。

## 3. 系统架构

```text
┌──────────────────────────────────────────────────────────────┐
│                    浏览器管理工作台                          │
│  建档 / 监测 / 行动清单 / 预案 / 试验 / 账户与安全          │
└───────────────────────────┬──────────────────────────────────┘
                            │ 本机 HTTP + Cookie + CSRF
                            ▼
┌──────────────────────────────────────────────────────────────┐
│ server.py：本机 API、权限控制、业务编排和静态页面服务        │
├──────────────┬───────────────┬──────────────┬────────────────┤
│ 账户与加密   │ 监测与证据     │ Agent/工作流 │ 预案版本与导出 │
│ auth.py      │ monitoring.py │ engine.py    │ response_plans │
│ secure_store │ live_data.py  │ workflow.py  │                │
└──────┬───────┴───────┬───────┴──────┬───────┴────────────────┘
       │               │              │
       │               │ Base64 图片  │ 可选文字模型
       │               ▼              ▼
       │    ┌──────────────────┐  本机或已授权校内 HTTPS 服务
       │    │ V-FloodNet       │
       │    │ 本机 sidecar     │
       │    │ 127.0.0.1:8877   │
       │    └──────────────────┘
       │
       ▼
runtime/：DPAPI 加密数据库、配置、日志和监测输入目录
```

系统由两个隔离的 Python 环境组成：

1. `.venv`：运行主工作台，仅依赖 Pillow 和 Python 标准库。
2. `.venv-vfloodnet`：运行 PyTorch、Torchvision 和 V-FloodNet，避免深度学习依赖污染主业务环境。

## 4. 核心运行原理

### 4.1 场地与预案建模

场地资料采用一个包含 `site`、`weather`、`points` 和 `plans` 的 JSON 数据集：

- `site`：场地名称、范围、核验状态和授权记录。
- `weather`：历史回放、模拟情景或经过本机保存的实时气象参考。
- `points`：地下入口、楼宇入口、通道、室外设施和信息联络点。
- `plans`：适用灾种、点位类型、行动、责任角色、来源和有效期。

`engine.validate_dataset` 会检查时间必须带时区、点位 ID 唯一、预案引用有效、来源链接合法及状态值正确。场地或点位未核验时，监测结果仍会明确标记为测试或待核验范围。

### 4.2 气象参考

`live_data.py` 当前支持：

- Open-Meteo 模型估计与预报。
- NOAA Aviation Weather Center 的 METAR 机场观测。

项目同时登记以下官方候选来源，但不将网页入口冒充实时 API：

- [中国气象数据网](https://data.cma.cn/)：历史台风、站点降雨和相关产品申请；账户接口待授权。
- [中央气象台台风网](http://typhoon.nmc.cn/)：人工核对台风位置、路径和预报展示；机器接口待确认。
- [浙江省气象局](http://zj.cma.gov.cn/)：浙江属地预警与气象服务候选来源；正式接口待授权。
- [浙江省水利厅](https://slt.zj.gov.cn/)：雨量、水位、河道和水库候选来源；水雨情接口待授权。

已实现“浙江省气象灾害预警信号信息”历史归档连接器，资源编码为 `S330000TAB0220180412002514`。该资源按月更新，只用于历史回放和规则评估，不参与当前实时预警判断。

安全录入个人 `userSecret`：

```powershell
python .\security_cli.py set-secret ZJDATA_USER_SECRET
```

下载一页历史预警并使用Windows DPAPI加密保存：

```powershell
python -m integrations.zj_open_data.alerts --page 1 --page-size 200
```

无条件开放数据不需要 `appSecret`。如果后续使用受限开放资源，再通过 `security_cli.py set-secret ZJDATA_APP_SECRET` 单独录入。任何秘密值都不得放入命令参数、README、日志或Git仓库。

来源状态随 `/api/live` 的 `official_sources` 返回，完整核验记录见 `data/source_catalog.json` 和 `docs/25_官方气象与水利数据源.md`。

气象参考会保存数据时刻、获取时刻、来源地址、单位和有效期。它们用于背景展示，不被解释为校园现场积水证据，也不代表官方预警。

### 4.3 监测来源

监测来源分为：

- `camera`：摄像头图片或外部结构化视觉结果。
- `water_depth`：已经换算到本点位地面基准的厘米水深。

传输方式包括：

- `vision`：工作台抓取图片并调用视觉模型。
- `file`：定期读取 `runtime/monitor-input` 内的 JSON。
- `http`：读取本机或明确授权的校内 HTTPS JSON 接口。
- `push`：已登录的受控本机程序向 `/api/monitor/ingest` 提交观测。

默认监测参数：

```json
{
  "poll_seconds": 60,
  "depth_watch_cm": 2,
  "depth_alert_cm": 15,
  "camera_confidence": 0.8,
  "confirm_samples": 2,
  "sources": []
}
```

默认值是软件运行参数，不是正式管理阈值，必须根据现场制度和验收结果重新确认。

### 4.4 摄像头隐私处理

`camera_vision.py` 的处理顺序为：

1. 检查来源类型、地址、可信 origin、时间头和文件范围。
2. 验证抓拍时间没有过期或来自未来。
3. 限制文件大小、像素数量和图片类型。
4. 根据 EXIF 校正图片方向。
5. 在完整原图坐标中填黑人工配置的 `privacy_masks`。
6. 裁剪必要的 `roi`。
7. 重新编码为 PNG，去除 EXIF、文本和 ICC 等元数据。
8. 计算原始图片 SHA-256；相同画面不重复推理，也不刷新观测时间。
9. 仅把处理后的图片放入内存，通过 Base64 发送到视觉端点。

程序不自动识别人脸或车牌。隐私区域必须由现场人员根据固定视野人工配置。校内远程视觉服务必须设置非全画面 ROI；公网视觉服务被拒绝。

RTSP/RTSPS 网络直连当前停用。应由本机或校内网关输出带真实采集时间的 JPEG/PNG 抓拍。

### 4.5 V-FloodNet 推理

`integrations/v_floodnet/service.py` 将上游图像分割模型包装为只监听回环地址的 Chat Completions 兼容接口。

单张图片的处理流程为：

1. 解码图片并转换为 RGB。
2. 检查宽高和总像素数。
3. 缩放到 `416 × 416`。
4. 转为 0—1 张量并使用 ImageNet 均值和标准差归一化。
5. 在 CPU 或 CUDA 上执行模型推理。
6. 将输出转换为水体概率图，并插值回原图尺寸。
7. 使用默认像素概率阈值 `0.5` 计算水体像素占比。
8. 水体占比达到 `0.002`，即 0.2% 时输出 `water_detected: true`。

返回结构示例：

```json
{
  "water_detected": true,
  "confidence": 0.84,
  "quality": "good",
  "summary": "V-FloodNet分割水体像素占比约...，结果仅作为待人工核验线索。"
}
```

`confidence` 是当前输出概率分布推导的启发式把握程度，不是经过现场校准的准确率。主工作台不会保存模型返回的自由文字描述，而是根据结构化字段生成固定说明。

当前 sidecar 不接入上游人体、停牌、Detectron2、MeshTransformer 或厘米水深估计链路。

### 4.6 状态计算

每条观测都必须包含真实的 `observed_at`。系统根据有效期将来源标为：

- `fresh`：仍在有效时间窗内。
- `stale`：已经过期。
- `future`：设备时间晚于服务器。
- `poor`：画质、置信度或数据质量不足。
- `missing`：尚无观测。
- `error`：来源读取失败。
- `disabled`：来源已停用。

水深状态使用连续有效样本确认；摄像头状态要求连续独立画面，并受 `camera_confidence` 限制。一个点位出现相互冲突的有效来源时会明确标记冲突，而不是静默选择乐观结果。

### 4.7 Agent 与行动清单

规则 Agent 根据灾种和点位类型生成候选的“点位—预案”组合，再输出规范化行动卡。可选文字模型只能选择候选组合，输出中的非法引用、额外自由文字或未知任务会被拒绝。

行动卡支持确认、退回、状态更新和失效标记。修改已确认内容前必须先退回待核验，避免无痕覆盖。

### 4.8 预案草案

预案模块会把以下内容固化到版本快照中：

- 场地和点位核验状态。
- 使用的监测来源与观测时间。
- 当前人工校正。
- 选中的预案条目。
- 编辑人员、创建时间和内部复核记录。

保存操作采用递增修订号和预期版本检查，防止并发覆盖。历史版本可按标题、点位和复核状态检索，并可导出 HTML 或 JSON。

所有导出均明确标记为草案，不代表正式批准、发布或生效。

## 5. 功能模块

| 模块 | 功能 |
| --- | --- |
| 账户与安全 | 首次管理员初始化、管理员/操作员/观察员角色、密码修改、账户停用、会话过期和审计 |
| 试点建档 | 保存场地、点位、授权、预案条目和核验状态 |
| 最新气象 | 获取模型与机场参考，保存不可变快照供演练引用 |
| 监测总览 | 配置数据源、轮询、观测时效、点位状态、事件和人工校正 |
| 行动清单 | 根据规范预案生成待核验任务并记录状态流转 |
| 预案编制 | 预览、版本保存、历史搜索、内部复核和导出 |
| 公众提示草稿 | 整理已经确认的信息，不自动对外发布 |
| 试验工作台 | 计时、规则/模型对照、观察记录与流程评估 |
| 视觉验收 | 计算开发集和留出集指标，判断是否满足预设阈值 |
| 发布工具 | 生成干净源码包、逐文件 SHA-256 和整包校验值 |

## 6. 代码结构

```text
Laoyouzhunbei-v0.9-V-FloodNet/
├─ server.py                    本机 HTTP 服务、API 路由和业务编排
├─ auth.py                      本地账户、密码哈希、角色、会话与审计
├─ secure_storage.py            Windows DPAPI、文件权限和加密 SQLite
├─ instance_lock.py             同一 runtime 的单实例保护
├─ network_policy.py            URL 白名单、内网地址校验、DNS 钉住和 TLS
├─ engine.py                    数据校验、规则生成和可选文字模型选择
├─ campus_agent.py              场地准备度、范围确认和 Agent 入口
├─ workflow.py                  行动卡状态机
├─ monitoring.py                来源配置、采集、状态计算、事件和人工校正
├─ camera_vision.py             图片获取、时间校验、ROI、遮挡和视觉调用
├─ live_data.py                 Open-Meteo 与 METAR 解析、缓存和时效
├─ response_plans.py            预案快照、版本、复核、检索和导出
├─ experiments.py               试验计时与结果记录
├─ vision_acceptance.py         视觉模型离线验收
├─ evaluate_agent.py            Agent 评估入口
├─ doctor.py                    安装和运行环境自检
├─ security_cli.py              隐藏输入管理敏感配置
├─ build_release.py             干净源码发布包构建
├─ setup.ps1                    主工作台环境安装
├─ start.ps1 / start.cmd        主工作台启动入口
├─ start-with-vfloodnet.ps1     V-FloodNet 与工作台联合启动
├─ integrations/
│  └─ v_floodnet/
│     ├─ service.py             本机视觉 sidecar
│     ├─ setup.ps1              独立 PyTorch 环境安装
│     └─ requirements.txt       sidecar 额外依赖
├─ web/
│  ├─ index.html                单页工作台结构
│  ├─ app.js                    通用页面逻辑、请求和行动工作台
│  ├─ monitor.js                监测配置与展示
│  ├─ pilot.js                  试点建档
│  ├─ response_plans.js         预案编制、历史和复核
│  ├─ workflow.js               行动卡交互
│  ├─ security.js               登录、账号与安全设置
│  ├─ draft_guard.js            未保存草稿保护
│  └─ style.css                 页面样式
├─ data/
│  ├─ demo.json                 合成演练数据
│  ├─ zju_pilot.json            紫金港待确认试点模板
│  ├─ source_catalog.json       已核查的公开数据来源说明
│  ├─ evaluation/               Agent 与视觉验收模板
│  └─ experiment_forms/         试验记录 CSV 模板
├─ docs/                        安全、安装、摄像头、监测、验收和试点文档
├─ tests/                       后端与浏览器测试
├─ third_party/
│  ├─ V-FloodNet-SOURCE.txt     上游来源、提交和授权说明
│  └─ V-FloodNet/               上游源码快照和当前模型权重
├─ RELEASE_MANIFEST.json        原始源码交付清单
├─ DEPLOYMENT_MANIFEST.json     含权重部署版逐文件清单
└─ README_部署版_含模型权重.md  快速部署与迁移手册
```

`third_party/V-FloodNet/` 中还保留了上游训练、视频分割和水深研究代码，但主工作台当前只调用 `link_efficientb4_model.pth` 图像分割模型。

## 7. 运行时目录

首次安装和启动后会生成 `runtime/`。主要内容包括：

```text
runtime/
├─ runs.sqlite3                 加密的业务数据库
├─ auth.json                    加密的账户与审计状态
├─ secrets.dpapi                加密的端点、密钥和可信 origin
├─ monitor-config.json          加密的监测配置
├─ monitor-input/               受控本机图片或 JSON 输入目录
├─ setup-code.txt               首次初始化临时设置码
├─ vfloodnet-service.log        sidecar 标准输出
└─ vfloodnet-service-error.log  sidecar 错误日志
```

文件名可能随版本调整，应以实际运行结果为准。`runtime` 不属于公共发布内容。

## 8. API 概览

主要 API 分组：

| 路径 | 用途 |
| --- | --- |
| `/api/health` | 主服务健康检查 |
| `/api/auth/*` | 初始化、登录、登出、密码和用户管理 |
| `/api/pilot` | 试点资料读取和保存 |
| `/api/live` | 获取气象参考 |
| `/api/live/reference` | 把实时参考固化为可审计快照 |
| `/api/monitor` | 获取监测快照 |
| `/api/monitor/config` | 管理员保存监测来源和阈值 |
| `/api/monitor/ingest` | 受控程序推送结构化观测 |
| `/api/monitor/correction` | 人工校正点位状态 |
| `/api/monitor/refresh` | 请求立即刷新来源 |
| `/api/generate` | 规则或模型生成行动清单 |
| `/api/workflow` | 行动卡状态变更 |
| `/api/response-plans*` | 预览、保存、检索、复核和导出预案 |
| `/api/trials*` | 试验开始、停止和结果记录 |

除首次初始化和登录外，受保护 API 需要登录 Cookie 与 `X-App-Token` CSRF Token。写请求还必须使用 JSON Content-Type，并带与本机服务一致的 `Origin`。设备推送目前没有独立匿名令牌，应使用受控的本机操作员会话。

### 8.1 摄像头结构化推送

```json
{
  "source_id": "gate-camera",
  "observations": [
    {
      "observed_at": "2026-10-07T12:00:00+08:00",
      "water_detected": true,
      "confidence": 0.92,
      "quality": "good"
    }
  ]
}
```

`quality: poor` 时 `water_detected` 可以为 `null`；否则必须为布尔值。视觉结果不得提交 `depth_cm`。

### 8.2 水深推送

```json
{
  "source_id": "gate-depth",
  "observations": [
    {
      "observed_at": "2026-10-07T12:00:00+08:00",
      "depth_cm": 3.2,
      "depth_reference": "ground",
      "quality": "good"
    }
  ]
}
```

水深必须相对于本点位地面基准完成标定，不能直接使用河道海拔水位、设备距离或未经换算的原始值。

## 9. 部署要求

推荐配置：

- Windows 10/11 64 位。
- Python 3.11 64 位。
- CPU 模式建议至少 8 GB 内存。
- CUDA 模式需要兼容 CUDA 12.1 的 NVIDIA 显卡和驱动。
- CPU 部署建议预留至少 5 GB；CUDA 部署建议预留 10—15 GB。
- 默认端口：工作台 `8765`，V-FloodNet `8877`。

主工作台依赖：

```text
Pillow==12.3.0
```

V-FloodNet 运行环境的核心依赖：

```text
torch==2.2.2
torchvision==0.17.2
segmentation-models-pytorch==0.2.0
numpy>=1.23,<2
Pillow>=9.3,<13
```

## 10. 在线部署

### 10.1 校验压缩包

```powershell
Get-FileHash -LiteralPath '.\涝有准备_完整项目_v0.9_V-FloodNet_含模型权重_完整README_v3.zip' -Algorithm SHA256
Get-Content -LiteralPath '.\涝有准备_完整项目_v0.9_V-FloodNet_含模型权重_完整README_v3.zip.sha256'
```

两个值必须一致。完整解压后，在项目目录执行以下步骤。

### 10.2 安装主环境

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

Python 没有加入 PATH 时：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\setup.ps1 `
  -PythonPath 'C:\Python311\python.exe'
```

### 10.3 安装 V-FloodNet CPU 环境

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\integrations\v_floodnet\setup.ps1 `
  -TorchMode cpu
```

### 10.4 安装 V-FloodNet CUDA 环境

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\integrations\v_floodnet\setup.ps1 `
  -TorchMode cu121
```

CPU 与 CUDA 二选一。显卡或驱动条件不明确时先使用 CPU 完成功能验证。

### 10.5 设置本次启动密钥

```powershell
$bytes = [byte[]]::new(32)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$env:V_FLOODNET_API_KEY = [Convert]::ToHexString($bytes)
```

密钥只存在于当前 PowerShell 会话，不要写入脚本、README 或 ZIP。

### 10.6 联合启动

CPU：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\start-with-vfloodnet.ps1 `
  -Device cpu
```

CUDA：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\start-with-vfloodnet.ps1 `
  -Device cuda
```

浏览器访问：

```text
http://127.0.0.1:8765/
```

### 10.7 首次管理员

首次启动会生成有效期 20 分钟的设置码：

```powershell
Get-Content -LiteralPath .\runtime\setup-code.txt
```

在网页输入设置码并创建管理员。密码至少 15 个字符。没有默认正式账号或默认密码。

## 11. 敏感配置

使用隐藏输入保存密钥：

```powershell
& .\.venv\Scripts\python.exe -X utf8 .\security_cli.py set-secret VISION_ENDPOINT
& .\.venv\Scripts\python.exe -X utf8 .\security_cli.py set-secret VISION_API_KEY
& .\.venv\Scripts\python.exe -X utf8 .\security_cli.py set-secret VISION_MODEL
```

可用字段见 `.env.example`：

- `LLM_ENDPOINT`、`LLM_API_KEY`、`LLM_MODEL`。
- `VISION_ENDPOINT`、`VISION_API_KEY`、`VISION_MODEL`。
- `LLM_TRUSTED_ORIGINS`、`VISION_TRUSTED_ORIGINS`。
- `CAMERA_TRUSTED_ORIGINS`、`MONITOR_TRUSTED_ORIGINS`。
- `VISION_FFMPEG`。
- 数据保留天数。

联合启动脚本会临时注入本机 V-FloodNet 的视觉配置，因此使用内置 sidecar 时通常不需要永久保存 `VISION_*`。

## 12. 摄像头配置示例

```json
{
  "id": "gate-camera",
  "name": "入口地面抓拍",
  "point_id": "ZJG-P01",
  "kind": "camera",
  "transport": "vision",
  "input_type": "image_url",
  "url": "https://camera.school.internal/snapshots/gate.jpg",
  "enabled": true,
  "vision_enabled": true,
  "max_age_seconds": 300,
  "roi": [0.1, 0.45, 0.8, 0.5],
  "privacy_masks": [[0.1, 0.45, 0.2, 0.2]]
}
```

抓拍网关应返回：

```http
Content-Type: image/jpeg
X-Captured-At: 2026-10-07T12:30:00+08:00
Cache-Control: no-store
```

不要使用每次 HTTP 请求时间冒充拍摄时间。断线后返回旧图片不能成为新的现场证据。

## 13. 离线部署和跨电脑迁移

不应直接复制 `.venv` 或 `.venv-vfloodnet`。虚拟环境包含 Python 绝对路径、DLL 和硬件相关组件，应在目标电脑重新创建。

在联网的同架构 Windows 电脑准备 wheel：

```powershell
New-Item -ItemType Directory -Force -Path .\wheels | Out-Null
python -m pip download --only-binary=:all: --dest .\wheels -r .\requirements.txt
python -m pip download --dest .\wheels `
  --index-url https://download.pytorch.org/whl/cpu `
  torch==2.2.2 torchvision==0.17.2
python -m pip download --dest .\wheels `
  -r .\integrations\v_floodnet\requirements.txt
```

目标电脑安装 Python 3.11 后执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\setup.ps1 `
  -OfflineWheelDirectory 'D:\部署介质\wheels'

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\integrations\v_floodnet\setup.ps1 `
  -TorchMode cpu `
  -OfflineWheelDirectory 'D:\部署介质\wheels'
```

`runtime` 数据使用 Windows DPAPI CurrentUser 加密。把它复制到另一台电脑或另一个 Windows 账户通常无法解密。新电脑应重新创建管理员和密钥；历史数据迁移需要单独设计和验证。

## 14. 自检与测试

环境自检：

```powershell
& .\.venv\Scripts\python.exe -X utf8 .\doctor.py
& .\.venv\Scripts\python.exe -X utf8 .\doctor.py --json --skip-port
```

后端测试：

```powershell
& .\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p 'test_*.py' -v
```

当前部署包生成前已执行 254 项后端测试，全部通过；1 项符号链接测试因执行账户没有创建符号链接权限而跳过。这不等于真实设备、摄像头或现场模型已经验收。

浏览器测试使用 Node.js、Playwright 和 Edge，测试文件位于 `tests/browser_*.cjs`。不要对正式业务数据库运行写入测试。

## 15. 视觉验收

合成示例：

```powershell
& .\.venv\Scripts\python.exe -X utf8 .\vision_acceptance.py `
  --input .\data\evaluation\vision_acceptance_example.json `
  --output .\vision-example-report.json
```

合成样例只演示统计格式，不能证明模型在现场有效。正式验收需要：

1. 明确摄像机、点位、视野和使用范围。
2. 冻结开发集和独立留出集。
3. 覆盖白天、夜间、雨滴、反光、遮挡、无水、积水和不可用画面。
4. 以事件和画面指纹去重，避免连续视频帧造成数据泄漏。
5. 根据误报、漏报、未知率和点位差异确定阈值。
6. 先进行影子运行，再由管理方决定是否进入实际辅助流程。

## 16. 运维

### 正常停止

在联合启动窗口按 `Ctrl+C`。脚本会停止它启动的 V-FloodNet sidecar。

### 日志

```powershell
Get-Content -LiteralPath .\runtime\vfloodnet-service.log
Get-Content -LiteralPath .\runtime\vfloodnet-service-error.log
```

### 数据保留

默认保留时间：

- 观测：7 天。
- 监测事件：30 天。
- 人工校正：30 天。

预案版本、运行记录、建档和试验记录作为历史资料保留。实际保留期应按单位制度配置。

### 备份和升级

升级前应在原 Windows 账户下：

1. 正常停止服务。
2. 备份源码、`runtime` 和部署清单。
3. 验证备份可由原账户读取。
4. 保留原 `runtime`，只更新程序文件。
5. 完成自检和代表性业务验证后再恢复使用。

有 ZIP 副本不等于已经验证了加密业务数据的灾难恢复能力。

## 17. 安全模型

- 仅监听本机回环地址。
- 管理员、操作员和观察员分权。
- 密码使用 PBKDF2-HMAC-SHA256 加盐哈希。
- 登录会话有空闲和最长有效期，密码或账户变更会撤销相关会话。
- 写请求使用 Cookie、CSRF Token、Origin 和 Content-Type 校验。
- 数据库和敏感 JSON 使用 DPAPI CurrentUser 加密。
- 文件 ACL 限制为当前账户与 SYSTEM。
- 外部视觉、文字、摄像头和监测端点按用途分别授权。
- 非本机服务必须使用 HTTPS、可信证书和精确 origin。
- DNS 结果必须全部属于允许的私有地址范围；公网、元数据和链路本地地址被拒绝。
- 网络请求不使用系统代理、不跟随重定向，并限制响应大小和超时。
- 观察员响应会移除审核人、授权、路径、密钥和扩展字段等敏感信息。

本机加密不能防止已经控制该 Windows 账户的攻击者访问运行中的数据，也不能自动清除过去产生的明文副本、浏览器下载或外部备份。

## 18. 已知限制

- 当前只适合单机辅助工作台，不能直接作为局域网或公网多用户平台部署。
- 没有接入学校统一身份认证、CAS、LDAP 或 OAuth。
- 设备推送依赖操作员会话，没有单独的机器身份或最小权限 API Token。
- 不自动发送短信、邮件、企业微信、钉钉、广播或工单。
- 不自动识别人脸或车牌。
- RTSP 网络直连停用。
- V-FloodNet 只输出水体分割线索，不估计厘米水深。
- 默认阈值没有经过紫金港现场校准。
- 包内紫金港资料仍是待确认模板，不是学校批准的正式点位或预案。
- 当前上游权重通过完整 PyTorch 对象加载，只能使用已经核对来源和哈希的可信权重文件。

## 19. 文档索引

重点文档：

| 文档 | 内容 |
| --- | --- |
| `README_部署版_含模型权重.md` | 含权重部署、迁移和快速检查 |
| `docs/14_自动监测与识别.md` | 来源类型、推送协议和状态逻辑 |
| `docs/15_摄像头接入.md` | 抓拍、时间头、ROI、隐私和模型输出 |
| `docs/17_安全配置与使用边界.md` | 账号、加密、网络、保留期和迁移 |
| `docs/19_预案编制与内部复核.md` | 预案版本、权限、导出和资料缺口 |
| `docs/20_安装与交付.md` | 原始源码包安装和交付范围 |
| `docs/21_积水识别验收.md` | 现场样本、指标和阈值 |
| `docs/24_V-FloodNet本地接入.md` | 模型环境、权重、联合启动和授权边界 |

## 20. 授权与第三方代码

V-FloodNet 上游源码快照位于 `third_party/V-FloodNet/`，来源及提交信息记录于 `third_party/V-FloodNet-SOURCE.txt`，对应提交：

```text
60c30fbac9d264d020148780504871556285f455
```

上游仓库没有开源许可证文件，并声明 “All rights are reserved”。当前源码和权重用于经授权的本地试验与复现，不代表已经取得公开再分发或商业使用许可。

向第三方提供含源码或模型权重的部署包前，应由项目负责人确认授权范围。如果无法确认，应交付不含权重的干净源码包，并由使用方从官方来源自行取得模型文件。

## 21. 推荐上线顺序

1. 在隔离测试电脑完成 CPU 安装和自检。
2. 核验模型权重 SHA-256，并确认授权范围。
3. 用合成图片验证完整抓拍—推理—监测链路。
4. 由管理方确认试点楼宇、点位、角色和预案来源。
5. 接入带真实时间的摄像头网关或水深设备。
6. 配置 ROI、隐私遮挡、有效期和现场阈值。
7. 建立开发集和留出集，完成视觉验收。
8. 进行影子运行，不触发正式处置。
9. 核对误报、漏报、断流、过期和冲突处理。
10. 经管理方批准后，决定是否对接正式工单或信息发布流程。

在上述步骤完成前，本项目应保持为本机辅助、演练和研究验证工具。

## 22. 上线前核心数据补齐清单

当前项目的代码、V-FloodNet 接入和图像分割模型权重已经具备。进入真实现场试点前，主要缺口是现场业务数据、正式管理依据和模型验收证据。

### 22.1 优先级

| 优先级 | 数据项 | 当前状态 | 未补齐时的影响 |
| --- | --- | --- | --- |
| P0 | 真实场地和点位台账 | 当前为待确认模板 | 无法证明监测结果对应真实管理范围 |
| P0 | 现场标注图片开发集和留出集 | 只有合成示例 | 无法评估误报、漏报和现场适用性 |
| P0 | 摄像头或抓拍网关资料 | 未配置真实来源 | 无法形成实时视觉观测 |
| P0 | 正式预案和责任体系 | 当前为团队草稿 | 不能生成有管理依据的行动清单 |
| P0 | 模型验收指标和阈值 | 未经现场批准 | 无法决定模型能否进入影子运行 |
| P1 | 水深设备及地面基准 | 未接入 | 不能提供厘米水深 |
| P1 | 校园气象和官方预警 | 历史归档连接器已实现；实时接口未授权 | 可做官方历史回放，但当前预警仍只能依赖人工核验或后续实时接口 |
| P1 | 数据保留、备份和权限制度 | 待确认 | 无法形成正式数据治理流程 |
| P1 | 工单或信息发布接口 | 未接入 | 只能生成草案，不能进入正式发布流程 |
| P1 | 设备机器身份 | 未设计独立令牌 | 长期推送需要依赖操作员会话 |

### 22.2 真实场地和点位台账

场地资料至少需要：

- 场地唯一编号、正式名称、校区和试点边界。
- 授权状态、批准角色、授权资料编号和确认时间。
- 点位台账版本号。

每个点位至少需要：

- 稳定唯一的 `point_id`。
- 正式名称、点位类型和可定位的文字位置。
- 管理部门及管理边界。
- 已核实的风险特征和资料来源。
- 核验状态、核验角色和核验时间。
- 关联的摄像头编号和水深设备编号。

推荐点位表头：

```csv
point_id,point_name,point_type,location_description,management_boundary,risk_features,source_reference,status,verified_by_role,verified_at,camera_ids,sensor_ids
```

完成标准：每个点位都能由现场人员唯一定位，编号不重复，管理边界和资料来源清楚，管理方书面确认试点范围。

### 22.3 现场标注图片数据集

数据集应覆盖：

- 晴天、阴天、降雨、白天、夜间、逆光和弱光。
- 干燥地面、湿地面、反光、阴影、小面积积水和明显积水。
- 行人、车辆、落叶、井盖和其他容易误判的目标。
- 镜头雨滴、模糊、过曝、欠曝、遮挡和摄像头偏移。
- 断流、重复画面、旧画面和不可使用画面。

每张图片至少需要：

```csv
frame_id,camera_id,point_id,event_id,captured_at,sha256,label,label_basis,annotator_role,review_status,split,notes
```

其中：

- `label` 使用 `water`、`clear` 或 `unusable`。
- `captured_at` 必须是真实拍摄时间并带时区。
- `sha256` 用于去除重复图片。
- `event_id` 用于避免同一事件同时进入开发集和留出集。
- `split` 使用 `development` 或 `holdout`。

开发集用于分析和确定阈值；留出集在阈值冻结前不得用于反复调参。连续视频片段和同一事件不得跨集合分配。

### 22.4 摄像头和抓拍网关资料

每个来源需要确认：

- 摄像头编号及对应点位。
- 直接返回 JPEG/PNG 的抓拍地址。
- 网关认证方式；URL 中不得包含明文账号或密钥。
- `X-Captured-At` 或可信的 `Last-Modified` 时间字段。
- 实际更新间隔和最大有效期。
- 图片尺寸、编码格式、ROI 和隐私遮挡矩形。
- 设备维护部门、证书维护方和断流行为。

完成标准：抓拍地址不返回登录页或重定向；时间能真实反映旧图和断流；远端来源使用校内 HTTPS；ROI 和隐私遮挡已经现场复核。

### 22.5 正式预案和责任体系

每条预案至少需要：

- 稳定的 `plan_id`、正式标题和适用灾种。
- 适用点位类型或点位编号。
- 触发人工核验或行动的条件。
- 经批准的行动内容。
- 责任角色和复核角色。
- 正式制度、预案或会议纪要编号。
- 生效时间、到期时间和核验状态。

还必须明确谁可以：

- 确认入口和道路状态。
- 决定关闭入口或限制通行。
- 进行现场复核。
- 对外发布、撤回或更正信息。
- 在非工作时间接替相关职责。

### 22.6 模型验收指标

管理方和算法团队需要共同冻结：

- 积水类召回率最低要求。
- 无水类误报率上限。
- 不可用画面的正确拒绝率。
- 可接受的未知率。
- 单张图片最大响应时间。
- 摄像头断流和旧图的发现时间。
- 不同点位是否允许采用不同阈值。
- 影子运行期限、停止条件和复核周期。

验收报告必须记录模型权重哈希、数据集版本、阈值版本、摄像头范围、执行角色、复核角色、限制条件和下一次复核日期。

更换摄像头、视角、模型权重或关键阈值后必须重新验收。

### 22.7 水深和校园气象数据

如果需要厘米水深，每台设备应提供：

- `sensor_id`、`point_id`、型号、量程和精度。
- 地面基准定义和标定方法。
- 原始读数到 `depth_cm` 的换算公式。
- 校准记录、下一次校准日期、采样频率和断线行为。

河道海拔水位、设备距离和未经换算的原始读数不能直接作为 `depth_cm`。

校园气象和预警建议补齐本地雨量站、数据更新时间、单位、缺测含义、官方预警来源、适用区域、发布时间、撤销信息和数据使用许可。

### 22.8 数据治理和正式系统对接

管理方需要确认：

- 原始图片是否允许保存以及保存期限。
- 结构化观测、事件、人工校正和预案版本的保留期。
- 查看、编辑、导出、删除和备份权限。
- 备份频率、加密方式和恢复演练周期。
- Windows 账户变更、人员离岗和设备报废流程。
- 数据泄露、误报和错误发布的报告流程。

如需接入工单、企业微信、短信或广播，还需提供接口文档、审批节点、发布角色、撤回机制、消息模板、幂等编号、故障降级和测试环境。

正式发布不得仅由视觉模型直接触发，应保留人工审批和审计记录。

### 22.9 敏感数据边界

以下内容不得进入公共源码、README 或可公开分发的 ZIP：

- 摄像头账号、密码、Cookie、Token 和带密钥的 URL。
- 管理人员私人联系方式和不必要的个人身份信息。
- 未经授权的原始监控图片和视频。
- 内网拓扑、证书私钥和正式 API 密钥。
- 正式数据库、`runtime/secrets.dpapi` 和初始化设置码。

真实资料应存放在单位批准的受控目录中，不应放入公共项目的 `data/` 或 `docs/`。

### 22.10 推荐执行顺序

1. 确认一个经授权的试点楼宇和真实点位。
2. 确认点位对应的摄像头、抓拍时间和隐私范围。
3. 采集第一批无水、积水和不可用图片并完成人工复核。
4. 补齐对应的正式预案、责任角色和授权来源。
5. 冻结开发集、留出集和模型验收指标。
6. 完成摄像头、水深或气象来源接入测试。
7. 生成验收报告并申请进入影子运行。
8. 在影子运行期间记录误报、漏报、未知、断流和人工处理时间。
9. 完成数据治理、备份恢复和故障处置演练。
10. 经管理方批准后再决定是否连接正式工单或信息发布流程。

### 22.11 完成检查表

- [ ] 试点范围和授权资料已确认。
- [ ] 所有正式点位已唯一编号并完成管理边界核验。
- [ ] 摄像头和传感器已映射到真实点位。
- [ ] 抓拍时间、ROI、隐私遮挡、断流和旧图行为已测试。
- [ ] 开发集和留出集已经按事件隔离。
- [ ] 图片标签依据和复核状态完整。
- [ ] 模型阈值和验收指标已经冻结。
- [ ] 正式预案、责任角色和复核角色已经确认。
- [ ] 数据保留、导出、备份和恢复制度已经批准。
- [ ] 已完成规定期限的影子运行。
- [ ] 系统仍保留人工审批，不直接执行正式发布或设施控制。

最先应收集的五项资料是：一个经授权的试点楼宇及点位清单、摄像头抓拍和时间信息、第一批现场标注图片、对应的正式预案和责任角色，以及影子运行的最低验收指标。
