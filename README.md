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
- Python 3.11+
- KiCad 8（本仓库用 8.0.9 验证；安装后程序会**自动探测**符号库与 kicad-cli，无需手动配路径）
- 一个 OpenAI 兼容的 LLM API（可选：不配密钥可用 `--mock` 离线模式跑通全流程）

> **KiCad 路径自动探测**：程序会按「config.toml → 环境变量 → PATH → 各平台默认安装目录」的顺序自动找到 KiCad 8 的符号库和 `kicad-cli`，Windows / macOS / Linux 均适用。如果你的 KiCad 装在非标准位置，只需任选一种方式告诉程序：
> - 命令行加 `--symbols-dir "你的路径/share/kicad/symbols"`；或
> - 设置环境变量 `KICAD_SYMBOL_DIR`（符号库目录）和/或 `KICAD_INSTALL_DIR`（安装根目录）；或
> - 在 `config/config.toml` 的 `[kicad] install_dir` 里填安装根目录（TOML 路径用正斜杠 `/`）。

## 3. 安装步骤
```bash
git clone https://github.com/Xin4813/ai_agent_pcb.git
cd ai_agent_pcb
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

**只想离线跑通（无需密钥、无需 config.toml）**：直接跳到第 4 节执行 `--mock` 命令即可，最快 1 分钟看到原理图。

**想用真实 LLM**：复制配置模板并填入 API Key（`config.toml` 已被 .gitignore 忽略，不会提交）：
```bash
cp config/config.example.toml config/config.toml   # 然后填入你的 API Key
```

## 4. 运行命令
```bash
# 离线模式（无需密钥，用内置 Arduino 最小系统板知识库，推荐先跑通）
python -m src.cli "帮我设计一块 Arduino 最小系统板" --mock --out examples/output

# 真实 LLM 模式（需先在 config.toml 填好密钥）
python -m src.cli "帮我设计一块 Arduino 最小系统板" --out examples/output

# 交互修订：生成后输入指令，如 "把电容换成 100nF"，输入 exit 退出
python -m src.cli "帮我设计一块 Arduino 最小系统板" --mock -i
```
> Windows 下若用 PowerShell 换用反引号 `` ` `` 续行（或直接写在一行）；上面命令已是最简形式，直接复制即可运行。KiCad 符号库会自动探测，不需要 `--symbols-dir`。

网页版（可视化交互 + 自动生成 ERC/视觉/安全/功能四检报告）：
```bash
python web/server.py            # 然后浏览器打开 http://127.0.0.1:8000
```

> **修改代码或知识库模板后必须重启网页服务**：`web/server.py` 是常驻进程，Python 模块与内置模板在启动时一次性载入内存；之后只改文件、提交或 push，运行中的网站仍会用旧逻辑/旧模板生成原理图。重启后再用 `python tests/audit_full.py` 做一次五维回归，确认 ERC / 布局 / 安全 / 功能 / 拓扑修复均为 0。

> **网页偶发内部错误可追溯**：若生成失败只显示 `ValueError: too many values to unpack ...` 这类模糊消息，完整堆栈会写入项目根目录的 `server_error.log`，前端错误也会附带最后一个项目内代码帧的 `文件:行号`。反馈时把这一行贴出即可定位，不需要逐张截图试错。

> **ERC=0 ≠ 功能正确，接口要按完整连接集合核对**：曾出现 ERC/视觉/安全/功能五维全绿、但 USB 口漏接 GND（无回流地）、SWD 调试信号只挂 MCU 单脚（调试器接不上）却漏检的情况。现在功能自检会按接口标准核对：接了 USB D± 的连接器必须有 GND 脚、网名含 SWDIO/SWCLK 的信号必须引到排针；生成器也会自动补 USB 地、自动加 SWD 排针并改正「标 SWD 却接 USB」的错配标注。交付前仍建议用 `kicad-cli sch export netlist` 导出权威网表逐脚核对一遍。

## 5. ERC 电气规则校验
生成原理图后，可用 KiCad 命令行做校验（`src/kicad_writer.py` 已把符号库内嵌进 `.kicad_sch`，因此 `kicad-cli` 能独立解析）：
```bash
# 把 <生成的原理图> 换成 examples/output/schematic/ 下实际的 .kicad_sch 文件
kicad-cli sch erc "examples/output/schematic/<生成的原理图>.kicad_sch" ^
    --output "examples/output/schematic/erc.txt"
```
> 更省事的方式：直接用网页版 `python web/server.py`，每次生成会自动跑 ERC + 视觉 + 安全 + 功能四检，并在结果区显示徽章，无需手敲命令。

> 最小系统板只连接 VCC/GND/RESET/XTAL1/XTAL2/LED 等必要网络，其余 GPIO 故意留空。系统会自动为悬空引脚补 No-Connect 标志、为电源网络补驱动，因此最终 ERC 为 0 违规；把历史基线 63 条收敛到 0，正是"检查闭环"要展示的内容。若发现某个端口组（如 STM32 的 PB0–PB15）整组一个都没接、而同芯片其它端口已引出，系统会自动新增同针数排针并逐脚引出，无需人工截图指出。

ERC 只查电气，查不出"文字压线""导线穿过符号""旗标叠在一起"这类**排版重叠**。这部分由独立的布局自检负责（`src/visual_check.py`），与 ERC 互补：
```bash
# 一键扫描所有原理图的排版重叠，有重叠时退出码为 1
python tests/check_overlaps.py --all
# 只扫指定文件
python tests/check_overlaps.py "examples/output/schematic/LM1117稳压电源模块.kicad_sch"
```
检测范围：文字互压、导线穿文字/符号本体/PWR_FLAG 菱形、旗标互压、标题栏侵入、导线十字交叉。网页前端每次生成后会自动跑这项检查，在结果区显示"布局"徽章，点开可看详情——排版问题无需再靠人工截图发现。

## 6. 目录结构
- `src/`        Agent 全部代码（config / llm_client / knowledge / validator / symbol_lib / bom_generator / netlist_generator / kicad_writer / visual_check / agent / cli）
- `docs/`       设计报告与架构说明（`architecture.md`）
- `examples/`   生成的 BOM / 网表 / 原理图样例与 ERC 报告
- `config/`     `config.example.toml`（模板，已提交）与 `config.toml`（真实配置，已忽略）
- `tests/`      测试
- `assets/`     符号库等素材

## 7. AI 使用声明
- 本项目的代码骨架、KiCad S-expression 解析/生成逻辑、ERC 内嵌符号等关键实现，由 AI 辅助生成并在本地调试验证。
- 离线知识库（Arduino 最小系统板 BOM/网表）为内置示例数据，真实 LLM 模式下由模型根据自然语言需求生成。
