# AI Agent 电路板设计

> 课程大作业：用自然语言描述需求，由 AI Agent 自动产出 BOM、网表与 KiCad 原理图。

## 1. 项目简介
（一句话说明你做了什么）

## 2. 环境要求
- Python 3.10+
- KiCad 8
- 一个 OpenAI 兼容的 LLM API（密钥放 config.toml，不提交）

## 3. 安装步骤
1. 克隆仓库：`git clone https://github.com/Xin4813/ai_agent_pcb.git`
2. 创建并激活虚拟环境：`python -m venv venv` → `venv\Scripts\activate`（Windows）
3. 安装依赖：`pip install -r requirements.txt`
4. 配置密钥：复制 `config/config.example.toml` 为 `config/config.toml`，填入你的 API Key（该文件已被 .gitignore 忽略，不会提交）

## 4. 运行命令
（待补充：python -m src.agent "帮我设计一块 Arduino 最小系统板"）

## 5. 目录结构
- src/        Agent 代码
- docs/       设计报告等
- examples/   BOM / 网表 / 原理图样例
- tests/      测试
- assets/     符号库等素材

## 6. AI 使用声明
（待补充：哪些由 AI 生成、用了什么模型）
