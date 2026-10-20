# AI Agent 电路板设计

> 课程大作业：用一句自然语言描述需求，由 AI Agent 自动产出 **BOM（物料清单）→ 引脚级网表 JSON → KiCad 8 可直接打开的原理图（.kicad_sch）**，并支持交互式修订与 ERC 电气规则校验。

## 1. 项目简介
本项目实现一个「自然语言 → 电路板原理图」的 AI Agent 流水线：

1. **需求解析**：把"帮我设计一块 Arduino 最小系统板"这类句子，转成结构化 BOM。
2. **网表生成**：基于 BOM 与电路知识，生成引脚级连接关系（netlist JSON），带自纠错校验循环。
3. **原理图生成**：把网表自动绘制成 KiCad 8 原理图（`.kicad_sch`），并内嵌符号库、可被 KiCad 直接打开。
4. **ERC 校验**：通过 `kicad-cli sch erc` 做电气规则检查，形成"生成—检查—修订"闭环。
5. **交互修订**：用自然语言指令（如"把电容换成 100nF"）迭代修改设计。

## 2. 环境要求
- Python 3.11+（KiCad 8 配套脚本同样基于现代 Python）
- KiCad 8（已在 `C:/Users/Gray/AppData/Local/Programs/KiCad/8.0` 安装，本仓库用 8.0.9 验证）
- 一个 OpenAI 兼容的 LLM API（可选：不配密钥可用 `--mock` 离线模式跑通全流程）

## 3. 安装步骤
```bash
git clone https://github.com/Xin4813/ai_agent_pcb.git
cd ai_agent_pcb
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp config/config.example.toml config/config.toml   # 然后填入你的 API Key（已被 .gitignore 忽略，不会提交）
```

## 4. 运行命令
```bash
# 离线模式（无需密钥，用内置 Arduino 最小系统板知识库，推荐先跑通）
python -m src.cli "帮我设计一块 Arduino 最小系统板" --mock ^
    --symbols-dir "C:/Users/Gray/AppData/Local/Programs/KiCad/8.0/share/kicad/symbols" ^
    --out examples/output

# 真实 LLM 模式（需先在 config.toml 填好密钥）
python -m src.cli "帮我设计一块 Arduino 最小系统板" ^
    --symbols-dir "C:/Users/Gray/AppData/Local/Programs/KiCad/8.0/share/kicad/symbols" ^
    --out examples/output

# 交互修订：生成后输入指令，如 "把电容换成 100nF"，输入 exit 退出
python -m src.cli "帮我设计一块 Arduino 最小系统板" --mock -i ^
    --symbols-dir "C:/Users/Gray/AppData/Local/Programs/KiCad/8.0/share/kicad/symbols"
```

## 5. ERC 电气规则校验
生成原理图后，可用 KiCad 命令行做校验（`src/kicad_writer.py` 已把符号库内嵌进 `.kicad_sch`，因此 `kicad-cli` 能独立解析）：
```bash
kicad-cli sch erc "examples/output/schematic/Arduino_最小系统板__ATmega328P_PU_.kicad_sch" ^
    --output "examples/output/schematic/erc.txt"
```
> 最小系统板只连接了 VCC/GND/RESET/XTAL1/XTAL2/LED 等必要网络，其余 GPIO 引脚故意留空，因此 ERC 报告里会有若干"引脚未连接"类提示——这属于预期结果，正是"检查闭环"要展示的内容。

## 6. 目录结构
- `src/`        Agent 全部代码（config / llm_client / knowledge / validator / symbol_lib / bom_generator / netlist_generator / kicad_writer / agent / cli）
- `docs/`       设计报告与架构说明（`architecture.md`）
- `examples/`   生成的 BOM / 网表 / 原理图样例与 ERC 报告
- `config/`     `config.example.toml`（模板，已提交）与 `config.toml`（真实配置，已忽略）
- `tests/`      测试
- `assets/`     符号库等素材

## 7. AI 使用声明
- 本项目的代码骨架、KiCad S-expression 解析/生成逻辑、ERC 内嵌符号等关键实现，由 AI 辅助生成并在本地调试验证。
- 离线知识库（Arduino 最小系统板 BOM/网表）为内置示例数据，真实 LLM 模式下由模型根据自然语言需求生成。
