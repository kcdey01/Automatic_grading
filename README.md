# 自动阅卷系统

基于 AI 的自动化阅卷工具，支持截图识别、AI 评分、自动填分全流程。

## 功能特点

- **截图识别**：自动截取题目区域，调用 AI 视觉模型识别和评分
- **智能评分**：支持多家 AI 服务商（OpenAI、智谱 AI、通义千问、豆包、百度千帆、科大讯飞、小米 MiMo 等），自定义评分标准
- **多协议适配**：自动适配 OpenAI Chat Completions、火山引擎 Responses API、小米 MiMo `api-key` 认证等不同协议
- **自动生成评分标准**：支持从截图或上传题目图片，由 AI 自动生成评分标准并填入文本框，带结构化输出格式
- **规则调优**：收集 AI 评分记录，人工标记正确分数后利用大模型自动分析偏差模式，生成优化后的评分规则，一键应用
- **快捷优化建议**：输入优化想法，AI 分析后直接生成新评分标准，无需收集评分记录
- **自动填分**：基于坐标点击的自动填分，支持分数输入框、提交按钮、下一题按钮
- **空白卷预检**：基于截图灰度波动识别接近空白的答卷，可通过主界面百分比滑块调节敏感度并测试当前截图
- **批量处理**：支持批量阅卷，可设置总份数并自动停止
- **菜单栏操作**：所有低频操作（截图配置、配置保存、清空截图目录）移至菜单栏，主界面只保留核心操作
- **配置保存**：支持配置文件保存和加载，截图区域与按钮位置仅在手动点击「文件 → 保存配置」后写入配置文件
- **准备状态检查**：主界面显示截图区域、分数框、提交按钮、下一题按钮是否已设置
- **可滚动界面**：窗口缩小时内容可上下滚动，适应不同屏幕尺寸

## 系统架构

```
自动阅卷系统/
├── 上层GUI.py              # 主界面程序（启动入口）
├── 自动阅卷系统GUI.py      # 核心模块（流程编排）
├── modules/
│   ├── __init__.py
│   ├── 自动截图模块.py      # 截图区域选择与截图保存
│   ├── 自动评分模块.py      # AI 评分（支持多种模型）
│   ├── 自动填分模块.py      # 自动填分（坐标点击）
│   └── 规则调优模块.py      # 规则调优（分析偏差→生成新规则）
├── .gitignore             # Git 忽略规则
├── requirements.txt       # 依赖包列表
└── config.json            # 运行时配置文件（已从 Git 排除）
```

## 安装依赖

```bash
pip install -r requirements.txt
```

依赖包：
- Pillow >= 10.0.0（图像处理）
- pyautogui >= 0.9.54（自动化控制）
- tkinter（GUI，Python 内置）
- requests >= 2.31.0（HTTP 请求）
- zhipuai >= 2.1.0（智谱 AI SDK，仅选用智谱时安装）

## 使用方法

### 启动程序

```bash
python 上层GUI.py
```

### 配置步骤

1. **选择 AI 服务商**
   - 从下拉列表中选择：OpenAI、智谱 AI、阿里通义千问、字节豆包、零一万物、硅基流动、百度千帆、科大讯飞、小米 MiMo、自定义
   - 选定后自动填入 base_url 和建议模型，用户可继续编辑

2. **填写 API Key**
   - 标准服务商：直接填写 API Key
   - 百度千帆：填写 `API_Key:Secret_Key` 格式
   - 科大讯飞：填写 `appId:apiKey:apiSecret` 格式
   - 小米 MiMo：填写 Token Plan API Key（格式 `tp-xxxxx`）

3. **设置评分标准**
   - 示例：`标准答案：18.（1）-4；（2）9又九分之四。请根据标准答案评分，不要返回具体评分细节，只返回总分`

4. **截图配置（菜单栏 → 截图配置）**
   - 选择截图区域（拖拽框选题目区域）
   - 测试截图
   - 选择分数输入框位置
   - 选择提交按钮位置
   - 选择下一题按钮位置
   - 主界面「准备状态」会显示各项是否已设置
   - 这些位置选择后只更新当前运行配置，不会立即写入 `config.json`；需要点击「文件 → 保存配置」后才会持久保存

5. **生成评分标准（菜单栏 → 生成）**
   - 从截图生成：选择题目区域后自动截图，AI 分析生成评分标准
   - 从图片文件生成：选择本地题目图片，AI 分析生成评分标准
   - 生成的标准自动填入评分标准文本框

6. **开始阅卷**
   - 单题模式：处理当前题目
   - 批量模式：按设定份数循环处理，达到份数后自动停止
   - 开始按钮会根据当前模式显示「开始单题阅卷」或「开始批量阅卷」

7. **空白卷检测（可选）**
   - 主界面「空白阈值」滑块以百分比显示空白敏感度，越高越容易判定为空白
   - 内部实际阈值范围为 `0` 到 `40`，换算关系为：`实际阈值 = 百分比 / 100 * 40`
   - 默认实际阈值为 `15.0`，约等于 `38%` 空白敏感度
   - 点击「测试空白检测」会截取当前区域，显示灰度波动、当前阈值和判定结果，便于调节阈值

8. **规则调优（可选）**
   - 每次评分完成后自动收集记录到调优列表
   - 在列表中选中记录，填写正确分数并点击「标记」
   - 收集至少 1 条偏差记录后，点击「规则调优」调用大模型分析错误模式
   - 查看分析结果和优化后的评分规则
   - 点击「应用新规则」将优化后的规则写入评分标准输入框

9. **快捷优化建议（可选）**
   - 在「快捷优化建议」文本框输入优化想法
   - 点击「执行优化」，AI 分析后生成优化后的评分标准
   - 点击「应用新规则」将结果写入评分标准输入框

## 支持的 AI 服务商

| 服务商 | base_url 预设 | 默认模型 | 认证方式 | 协议 |
|---|---|---|---|---|
| OpenAI | api.openai.com | gpt-4o-mini | Bearer token | OpenAI Chat Completions |
| 智谱 AI | (SDK) | glm-4v | 智谱 SDK | 智谱 SDK |
| 阿里通义千问 | dashscope.aliyuncs.com/compatible-mode/v1 | qwen-vl-max | Bearer token | OpenAI Chat Completions |
| 字节豆包 | ark.cn-beijing.volces.com/api/v3 | doubao-seed-1-8-251228 | Bearer token | 火山引擎 Responses API |
| 零一万物 | api.lingyiwanwu.com/v1 | yi-vision | Bearer token | OpenAI Chat Completions |
| 硅基流动 | api.siliconflow.cn/v1 | Qwen/Qwen2-VL-72B-Instruct | Bearer token | OpenAI Chat Completions |
| 百度千帆 | qianfan.baidubce.com | ernie-4.0-8k | access_token | 自定义（access_token 鉴权） |
| 科大讯飞 | (签名认证) | spark-v4.0 | HMAC-SHA256 签名 | 自定义（签名认证） |
| 小米 MiMo | token-plan-cn.xiaomimimo.com/v1 | mimo-v2.5-pro | api-key | OpenAI Chat Completions |
| 自定义 | 用户填写 | gpt-4o-mini | Bearer token | OpenAI Chat Completions |

### 小米 MiMo 说明

小米 MiMo 使用 Token Plan 订阅模式，需注意：

- **API Key 格式**：`tp-xxxxx`（Token Plan 专用，与按量付费的 `sk-xxxxx` 不可混用）
- **认证方式**：使用 `api-key` 请求头（非标准 Bearer token）
- **集群选择**：可选中国/新加坡/欧洲集群，base_url 分别为：
  - 中国：`https://token-plan-cn.xiaomimimo.com/v1`
  - 新加坡：`https://token-plan-sgp.xiaomimimo.com/v1`
  - 欧洲：`https://token-plan-ams.xiaomimimo.com/v1`
- **订阅管理**：前往 [MiMo 订阅管理](https://platform.xiaomimimo.com) 获取 API Key

> 评分器均支持多模态图片理解，需选用支持 vision 的模型。

## 配置文件

配置文件 `config.json` 包含以下字段（已从 Git 跟踪中排除，避免泄露 API Key）：

```json
{
  "provider": "OpenAI",
  "api_key": "your_api_key",
  "model": "gpt-4o-mini",
  "base_url": "https://api.openai.com",
  "extra_headers_json": "",
  "criteria": "请根据标准答案评分，只返回分数",
  "batch_mode": false,
  "total_questions": "0",
  "blank_threshold": 15.0,
  "screenshot_region_norm": [0.1, 0.1, 0.9, 0.8],
  "score_input_pos": [100, 200],
  "submit_btn_pos": [180, 200],
  "next_btn_pos": [260, 200],
  "filler_mode": "pyautogui"
}
```

说明：
- `blank_threshold` 为实际空白检测阈值，主界面以百分比展示和调节
- `screenshot_region_norm` 为归一化截图区域坐标，格式为 `[left, top, right, bottom]`
- `score_input_pos`、`submit_btn_pos`、`next_btn_pos` 为屏幕坐标
- 截图区域和按钮位置选择后不会自动保存，需要手动点击「文件 → 保存配置」

## 注意事项

1. **截图区域**：确保选择的区域包含完整的题目内容
2. **API 密钥**：请妥善保管，不要提交到公开仓库（config.json 已配置在 .gitignore 中）
3. **网络连接**：确保可以访问所选 AI 服务商的 API
4. **模型选择**：推荐使用支持多模态的视觉模型（带 vl/vision 后缀）

## 故障排除

| 问题 | 解决方案 |
|------|----------|
| `No module named 'requests'` | `pip install requests` |
| API 连接失败 | 检查 API Key、网络连接、模型名称 |
| 火山引擎 403 | 检查方舟平台账户余额（日志会打印详细错误信息） |
| 小米 MiMo 认证失败 | 确认 API Key 格式为 `tp-xxxxx`，且订阅未过期 |
| 图片识别失败 | 检查截图区域、图片清晰度、评分标准 |
| 正常答卷被判为空白 | 降低「空白阈值」百分比，并点击「测试空白检测」确认判定结果 |
| 空白卷没有被识别 | 提高「空白阈值」百分比，并点击「测试空白检测」确认判定结果 |
| 填分失败 | 检查按钮位置配置 |
| 批量模式不停止 | 检查总份数是否设置为大于 0 |

## 更新日志

- **2026-05-11**：优化空白检测与主界面状态提示
  - 空白卷检测移除 `numpy` 依赖，改用 Pillow 计算灰度标准差
  - 新增空白阈值百分比滑块，支持调节空白敏感度
  - 新增「测试空白检测」按钮，显示灰度波动、当前阈值和判定结果
  - 主界面新增准备状态行，显示截图区域、分数框、提交按钮、下一题按钮是否已设置
  - 开始按钮根据单题/批量模式动态显示「开始单题阅卷」或「开始批量阅卷」
  - 截图区域和按钮位置选择后不再自动写入 `config.json`，仅在点击「文件 → 保存配置」后持久化
- **2026-05-09**：新增小米 MiMo 支持与火山引擎 Responses API 适配
  - 新增小米 MiMo 服务商（Token Plan 模式，`api-key` 认证头）
  - 适配火山引擎方舟 Responses API（`/api/v3/responses`），请求/响应格式自动切换
  - 统一 LLM 调用函数 `call_llm_text`，自动适配不同协议
  - 新增 API 错误响应体日志打印，便于诊断 403 等问题
  - 更新字节豆包默认模型为 `doubao-seed-1-8-251228`
  - 修复 URL 拼接逻辑，支持 `/v3` 等版本号
- **2026-05-07**：菜单栏优化与自动生成评分标准
  - 新增菜单栏，将截图配置、保存/加载配置、清空截图目录等低频操作移至菜单
  - 主界面 runbox 精简为：开始、停止、清空日志、进度状态
  - 新增「生成」菜单，支持从截图或图片文件自动生成评分标准
  - 生成评分标准时自动压缩图片、添加结构化输出格式，提高 AI 识别率
  - extract_score 增加「预估得分」「最终得分」等摘要模式，优先匹配总分而非中间小分
  - 评分标准末尾自动追加输出格式指令，AI 评分响应以「最终得分：X分」收尾
  - 新增「快捷优化建议」面板，输入优化想法即可 AI 生成新评分标准
- **2026-05-06**：新增规则调优功能与可滚动界面
  - 新增规则调优模块，自动收集评分记录，利用大模型分析偏差模式生成优化后的评分规则，支持一键应用
  - AutoScoringSystem 新增 on_score_callback 回调钩子，每次评分后自动记录
  - 新增规则调优 UI 面板：评分记录列表、人工标记正确分数、调优触发、结果展示、应用新规则
  - 界面改为 Canvas + Scrollbar 可滚动布局，适应小屏窗口
- **2026-04-27**：新增多服务商支持
  - 支持 OpenAI、智谱 AI、阿里通义千问、字节豆包、零一万物、硅基流动、百度千帆、科大讯飞
  - 自定义百度千帆和科大讯飞的鉴权评分器
  - 移除 DOM 自动化填分模式，仅保留坐标点击
  - 批量模式添加份数限制自动停止
  - 添加 .gitignore，排除 config.json 和缓存文件
- **初始版本**：基础截图、AI 评分、自动填分功能

## 许可证

MIT License
