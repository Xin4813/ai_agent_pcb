# STORY.md — AI Agent 电路板设计系统 · 大作业答辩 PPT

## ① 用户意图对齐
- **目标受众**：课程答辩老师与同学（现场演讲 + 评审）
- **核心目标**：证明本系统能把一句自然语言需求（"帮我设计一块 Arduino 最小系统板"）自动转化为可被 KiCad 打开的电路原理图，跑通"环境→BOM→引脚级网表→KiCad 原理图→ERC→交互式修订"全流程，并讲清其中的 KiCad 8 关键技术难点。
- **PPT 长度**：18 页（封面 + 目录 + 6 章节扉页 + 9 内容页 + 致谢）
- **视觉调性**：学术严谨 / 蓝白克制 / 证据驱动 / 流程清晰
- **内容边界**：必讲五步流水线、KiCad 8 五大技术点、ERC 结果；不讲完整代码逐行、不讲商业对比。

## ② 页面布局骨架
- 总页数：**18**。Hero 页 4 个（封面 / 五步总览 / ERC 结果 / 致谢），占 22%，满足 20–30%。
- 目录↔章节契约：目录声明 **6 章**，全篇恰有 6 个 `type: section` 扉页（03/05/07/11/13/16），编号 01–06 连续，与目录逐字对应。
- 非对称版式 ≥ 40%：封面骑线、左标题+右内容、流程图、巨型数字、矩阵等均为非对称；仅章节扉页用对称大字版式（计 6 页，含在允许范围）。
- N卡片横排：全篇 ≤2 页（技术点矩阵用 2 列非等宽+图标，不计入横排）。
- Hero 不相邻，间隔 ≥1 个 Supporting。

## ③ 页面大纲

| # | title | type | role | rhythm | layout | visual | visual_role | anti_pattern |
|---|-------|------|------|--------|--------|--------|-------------|--------------|
| 1 | 封面 | cover | hero | peak | 全屏深蓝顶条+骑线大标题 | L1 深蓝顶条+主标 | anchor | 禁党政红金/印章/人物大头照 |
| 2 | 目录 | catalog | supporting | transition | 左深蓝栏+编号列表 | L3 编号 | — | 禁圆球编号/书法数字 |
| 3 | 01 项目背景与作业要求 | section | transition | transition | 大数字+标题+子要点 | L1 大数字 01 | anchor | 禁满版蓝红金 |
| 4 | 项目背景与作业要求 | content | supporting | valley | 左标题栏+右内容卡 | L3 图标 | evidence | 禁等宽四卡 |
| 5 | 02 系统总体架构 | section | transition | transition | 大数字+标题 | L1 大数字 02 | anchor | 同 03 |
| 6 | 系统总体架构 | content | supporting | valley | 上 SVG 架构图+下说明卡 | L1 SVG 流程图 | evidence | 禁装饰小图 |
| 7 | 03 五步基础任务流水线 | section | transition | transition | 大数字 | L1 大数字 03 | anchor | 同 03 |
| 8 | 五步流水线总览 | content | hero | peak | 横向 5 节点流程 SVG | L1 SVG 流程 | anchor | 禁 N卡片横排 |
| 9 | 步骤1-3：环境/NL→BOM/网表 | content | supporting | valley | 左标题+右三卡 | L2 图标卡 | evidence | 禁对称等分 |
| 10 | 步骤4-5：原理图/交互修订 | content | supporting | valley | 非对称双栏 | L2 代码/指令卡 | evidence | 禁上下等分 |
| 11 | 04 KiCad 8 关键技术点 | section | transition | transition | 大数字 | L1 大数字 04 | anchor | 同 03 |
| 12 | KiCad 8 五大关键技术点 | content | supporting | valley | 2 列图标矩阵 | L2 FAIcon | evidence | 禁纯文字堆 |
| 13 | 05 ERC 验证与结果 | section | transition | transition | 大数字 | L1 大数字 05 | anchor | 同 03 |
| 14 | ERC 验证结果分析 | content | hero | peak | 巨型数字+洞察 | L1 大数字 63/0 | anchor | 禁把数字塞角落 |
| 15 | 交互式修订演示 | content | supporting | valley | 上下 before/after | L2 对比卡 | evidence | 禁 50:50 等分 |
| 16 | 06 进阶挑战与总结 | section | transition | transition | 大数字 | L1 大数字 06 | anchor | 同 03 |
| 17 | 进阶挑战与总结展望 | content | supporting | valley | 左标题+右清单 | L3 图标 | evidence | 禁等宽四卡 |
| 18 | 致谢 | ending | hero | peak | 居中致谢+系统标记 | L3 | anchor | 禁烟花/金装饰 |

## 数据落点（关键页）
- P14 ERC：63 条违规 / exit 0 —— 说明：最小系统板故意留空 GPIO、未放电源符号，63 条是**预期**结果，恰好证明"ERC 检查循环"已闭环，工具链本身 exit 0 无崩溃。
- P15 修订：电容 ['22pF','22pF','100nF'] → ['100nF','100nF','100nF']，重渲染后 ERC 仍 exit 0。
- P8 五步：①环境 ②NL→BOM ③BOM→引脚级网表 ④网表→KiCad 原理图 ⑤交互式修订 —— 对应作业 40 分基础档。
