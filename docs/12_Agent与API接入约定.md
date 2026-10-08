# 紫金港行动编排Agent与API接入约定

版本v0.4，2026-10-02。代码入口为`campus_agent.run_agent`；模型匹配适配器为`engine.select_with_model`。当前没有选择服务商，没有配置真实模型，也没有实际付费调用记录。

## 模块边界

| 文件 | 职责 |
|---|---|
| `campus_agent.py` | 编排、准入、阶段限制、输入指纹、证据引用 |
| `engine.py` | 字段校验、范围检索、固定规则、模型HTTP请求与严格响应检查 |
| `live_data.py` | 独立公开天气连接器、时效与来源、不可变快照 |
| `workflow.py` | 人工确认、状态流转、提示草稿、反馈和失效 |
| `experiments.py` | 输入冻结、条件检查、计时、评分与完成时快照 |
| `evaluate_agent.py` | 独立案例批量评估，默认仅规则模式 |

一次AI运行最多进行一次模型HTTP请求；温度0、45秒超时、响应上限1MiB；不会自动重试、切换服务或静默降级。用户可明确选择规则方式重新运行并另记试验。模型无发邮件、发布通知、执行命令、控制设备或自由访问URL的工具权限。

## 待提供的API信息

1. 服务商与官方协议文档链接。
2. 完整HTTPS对话补全地址，不能仅提供服务商主页。
3. 可调用的模型名称及是否支持温度参数、JSON内容输出。
4. 速率、账户额度、数据使用要求与是否支持本地部署。
5. 密钥在本机填写，不在聊天或网页输入。

将`.env.example`复制为`.env`后填写：

```dotenv
LLM_ENDPOINT=https://服务商提供的完整接口
LLM_API_KEY=仅在本机填写
LLM_MODEL=账户可调用的模型名
```

当前只支持下述通用对话补全契约。其他协议需另写适配器并通过合成数据测试后启用；不提前声称任意API均可直接兼容。

## 外发字段与模型输出

只发送`site`的范围字段、`weather`资料字段、`points`任务相关字段、`plans`条款字段及`allowed_pairs`。`site.authorization`、`verified_by`、`verified_at`、反馈、试验参与者、评分以及其他非白名单扩展字段不会自动外发。

浏览器“检查资料与发送范围”调用本机`/api/agent/inspect`，显示白名单预览，返回`external_call_made:false`。该预览不是脱敏识别器：如果原文/名称/来源本身含个人信息，仍须自行删除或取得明确许可。

模型必须返回：

```json
{"matches":[{"point_id":"ZJG-P01","plan_id":"ZJG-R01"}]}
```

任何额外字段、未允许编号、类型不符或非JSON内容均被拒绝；任务文字和责任角色由服务器从预案原文组装。允许空匹配，但会记录未选候选与人工复核提醒，不能解释成安全。

输入里的自由文本均被视为数据，系统提示明确要求忽略伪指令。主要约束由服务器白名单和引用校验落实，而非只依赖提示词。

## 本机接口

所有POST使用`Content-Type: application/json`及bootstrap返回的`X-App-Token`，只接受本机Host及允许的Origin。最大请求500KB。令牌不等于用户身份认证。

| 接口 | 核心字段 | 返回/约束 |
|---|---|---|
| GET `/api/bootstrap` | 无 | 保存的试点资料、模板、AI配置布尔值、令牌；不返回密钥 |
| POST `/api/agent/inspect` | `data` | 准入检查、指纹、外发预览；不调用模型 |
| POST `/api/pilot` | `action:save, revision, data` | 保存杭州紫金港资料，拒绝旧版本覆盖 |
| POST `/api/generate` | `data, mode, scope_confirmed, external_data_confirmed, operation_mode, trial_id?` | 桌面/影子；AI需资料外发确认；可绑定计时试验 |
| POST `/api/workflow` | `run_id, revision, operation`及对应字段 | 审核、草稿、反馈，版本控制 |
| POST `/api/trials/start` | `participant, scenario_id, condition, data` | 保存输入快照和开始时间 |
| POST `/api/trials/stop` | `trial_id` | 停止任务计时并固定当时输出 |
| POST `/api/trials/finish` | `trial_id, outcome, evaluator, notes`及四项计数 | 人工评分封存；完成须先停止计时 |
| POST `/api/trials` | 空对象 | 最近100次试验，包含活动/待评分/完成/中止记录 |

`operation_mode`可选`tabletop`或`shadow`；影子运行需五项准入通过和新近本机天气快照。`mode`为`rules`或`ai`。计时条件为`manual`、`rules`、`ai`，生成方式和输入指纹必须与计时条件吻合；每次试验最多绑定一个成功生成。

## API到位后的验收顺序

1. 完全合成资料单次请求；核对实际服务商和模型，记录响应及usage。
2. 错误密钥、超时、非JSON、额外字段、无效点位、资料伪指令、空匹配七类失败测试。
3. 运行12个开发案例，修复后冻结接口与提示词；以新文件名保留每轮结果。
4. 管理方确认脱敏/许可，再运行独立留出集和用户试验。

现存记录包括成功且通过校验的模型原始匹配JSON；原始失败响应没有归档，不把这一功能写成已实现。失败原因通过调用方报错和评估报告保留，费用以服务商真实账单为准。

## 生产部署缺口

本机版本无真正用户身份、权限分层和校内单点登录，也没有多人实时同步或正式发布审签。真实多用户部署前需补认证、最小权限、HTTPS、备份恢复、运维责任与日志访问控制；此前保持单机主持式试验，不把127.0.0.1服务改成公网绑定当作上线。
