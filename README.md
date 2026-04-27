# 自动阅卷系统

基于 AI 的自动化网上阅卷工具，支持截图识别、AI 评分、自动填分全流程。

## 功能特点

- **截图识别**：自动截取题目区域，调用 AI 模型识别题目和答案
- **智能评分**：支持多种 AI 服务商（智谱 AI、火山引擎等），自定义评分标准
- **自动填分**：支持坐标点击和 DOM 自动化两种填分方式
- **批量处理**：支持批量阅卷，可设置总题数并实时显示进度
- **配置保存**：支持配置文件保存和加载，避免重复配置

## 系统架构

```
自动阅卷系统/
├── 上层GUI.py              # 主界面程序（启动入口）
├── 自动阅卷系统GUI.py      # 核心模块（流程编排）
├── modules/
│   ├── __init__.py
│   ├── 自动截图模块.py      # 截图区域选择与截图保存
│   ├── 自动评分模块.py      # AI 评分（支持多种模型）
│   └── 自动填分模块.py      # 自动填分（坐标点击/DOM）
├── 使用指南.md              # 详细使用说明
├── requirements.txt         # 依赖包列表
└── config.json             # 配置文件（运行时生成）
```

## 安装依赖

```bash
pip install -r requirements.txt
```

依赖包：
- Pillow >= 9.0.0（图像处理）
- pyautogui >= 0.9.53（自动化控制）
- tkinter（GUI）
- zhipuai >= 2.0.0（智谱 AI SDK）
- requests（HTTP 请求）

## 使用方法

### 启动程序

```bash
python 上层GUI.py
```

### 配置步骤

1. **选择 AI 服务商**
   - OpenAI 兼容 API（支持自建服务）
   - 智谱 AI（glm-4v 等多模态模型）

2. **填写 API Key**
   - 在对应服务商获取 API Key

3. **设置评分标准**
   - 示例：`请根据标准答案评分，只返回分数`

4. **一次性配置**
   - 选择截图区域（拖拽框选题目区域）
   - 选择分数输入框位置
   - 选择提交按钮位置
   - 选择下一题按钮位置

5. **开始阅卷**
   - 单题模式：处理当前题目
   - 批量模式：按总题数循环处理

## 支持的 AI 模型

### 智谱 AI
- `glm-4v`（推荐，支持图片）

### 火山引擎
- `doubao-seed-2-0-pro-260215`（推荐，支持图片）
- `skylark2-pro`（支持图片）
- `skylark2-lite`（轻量级）

### OpenAI 兼容
- `gpt-4o-mini`（默认）
- `gpt-4o` 等支持图片的模型

## 配置文件

配置文件 `config.json` 包含以下字段：

```json
{
  "provider": "OpenAI兼容",
  "api_key": "your_api_key",
  "model": "gpt-4o-mini",
  "base_url": "https://api.openai.com",
  "criteria": "请根据标准答案评分，只返回分数",
  "batch_mode": false,
  "total_questions": "0",
  "filler_mode": "坐标点击",
  "dom_target_url": "",
  "dom_browser_mode": "新开浏览器",
  "dom_cdp_url": "",
  "dom_score_selector": "",
  "dom_submit_selector": "",
  "dom_next_selector": ""
}
```

## 注意事项

1. **截图区域**：确保选择的区域包含完整的题目内容
2. **API 密钥**：请妥善保管，不要提交到公开仓库
3. **网络连接**：确保可以访问所选 AI 服务商的 API
4. **模型选择**：推荐使用支持多模态的模型进行图片理解

## 故障排除

| 问题 | 解决方案 |
|------|----------|
| `No module named 'requests'` | `pip install requests` |
| API 连接失败 | 检查 API Key、网络连接、模型名称 |
| 图片识别失败 | 检查截图区域、图片清晰度、评分标准 |
| 填分失败 | 检查按钮位置配置、浏览器兼容性 |

## 更新日志

- **2026-04-27**：初始版本发布
  - 支持截图识别、AI 评分、自动填分
  - 支持智谱 AI、火山引擎、OpenAI 兼容 API
  - 支持批量处理模式
  - 支持配置保存和加载

## 许可证

MIT License