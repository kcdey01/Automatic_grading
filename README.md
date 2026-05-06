# 自动阅卷系统

基于 AI 的自动化阅卷工具，支持截图识别、AI 评分、自动填分全流程。

## 功能特点

- **截图识别**：自动截取题目区域，调用 AI 视觉模型识别和评分
- **智能评分**：支持多家 AI 服务商（OpenAI、智谱 AI、通义千问、豆包、百度千帆、科大讯飞等），自定义评分标准
- **规则调优**：收集 AI 评分记录，人工标记正确分数后利用大模型自动分析偏差模式，生成优化后的评分规则，一键应用
- **自动填分**：基于坐标点击的自动填分，支持分数输入框、提交按钮、下一题按钮
- **批量处理**：支持批量阅卷，可设置总份数并自动停止
- **配置保存**：支持配置文件保存和加载，避免重复配置
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
- Pillow >= 9.0.0（图像处理）
- pyautogui >= 0.9.53（自动化控制）
- tkinter（GUI，Python 内置）
- requests（HTTP 请求）
- zhipuai >= 2.0.0（智谱 AI SDK，仅选用智谱时安装）

## 使用方法

### 启动程序

```bash
python 上层GUI.py
```

### 配置步骤

1. **选择 AI 服务商**
   - 从下拉列表中选择：OpenAI、智谱 AI、阿里通义千问、字节豆包、零一万物、硅基流动、百度千帆、科大讯飞、自定义
   - 选定后自动填入 base_url 和建议模型，用户可继续编辑

2. **填写 API Key**
   - 标准服务商：直接填写 API Key
   - 百度千帆：填写 `API_Key:Secret_Key` 格式
   - 科大讯飞：填写 `appId:apiKey:apiSecret` 格式

3. **设置评分标准**
   - 示例：`标准答案：18.（1）-4；（2）9又九分之四。请根据标准答案评分，不要返回具体评分细节，只返回总分`

4. **一次性配置**
   - 选择截图区域（拖拽框选题目区域）
   - 选择分数输入框位置
   - 选择提交按钮位置
   - 选择下一题按钮位置

5. **开始阅卷**
   - 单题模式：处理当前题目
   - 批量模式：按设定份数循环处理，达到份数后自动停止

6. **规则调优（可选）**
   - 每次评分完成后自动收集记录到调优列表
   - 在列表中选中记录，填写正确分数并点击「标记」
   - 收集至少 1 条偏差记录后，点击「规则调优」调用大模型分析错误模式
   - 查看分析结果和优化后的评分规则
   - 点击「应用新规则」将优化后的规则写入评分标准输入框

## 支持的 AI 服务商

| 服务商 | base_url 预设 | 默认模型 | 评分器 |
|---|---|---|---|
| OpenAI | api.openai.com | gpt-4o-mini | OpenAI 兼容 |
| 智谱 AI | (SDK) | glm-4v | 智谱 SDK |
| 阿里通义千问 | dashscope.aliyuncs.com/compatible-mode/v1 | qwen-vl-max | OpenAI 兼容 |
| 字节豆包 | ark.cn-beijing.volces.com/api/v3 | doubao-vision-pro-32k | OpenAI 兼容 |
| 零一万物 | api.lingyiwanwu.com/v1 | yi-vision | OpenAI 兼容 |
| 硅基流动 | api.siliconflow.cn/v1 | Qwen/Qwen2-VL-72B-Instruct | OpenAI 兼容 |
| 百度千帆 | qianfan.baidubce.com | ernie-4.0-8k | 自定义（access_token 鉴权） |
| 科大讯飞 | (签名认证) | spark-v4.0 | 自定义（HMAC-SHA256 签名） |
| 自定义 | 用户填写 | gpt-4o-mini | OpenAI 兼容 |

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
  "filler_mode": "pyautogui"
}
```

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
| 图片识别失败 | 检查截图区域、图片清晰度、评分标准 |
| 填分失败 | 检查按钮位置配置 |
| 批量模式不停止 | 检查总份数是否设置为大于 0 |

## 更新日志

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