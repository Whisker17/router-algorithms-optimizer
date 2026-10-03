# Handoff：路由算法现状评估与下一步探索

> **接手方**：一个独立 agent，任务是评估现有实现，并提出新的算法方向和优化点。
>
> **基线**：仓库 `router-algorithms-optimizer`，分支 `dev`。本文的数字、路径和章节号都已对照
> `dev` @ `358bbe0c0a86965607c950d12ee0ce0e84915b2d` 核对过。
> 本文本身是只改文档的提交，不改变任何源码、配置或冻结证据。它自己的合并提交可以用
> `git log -1 -- docs/handoff/algorithm-exploration-handoff.md` 查到。
>
> **定位**：本文只做导航和提供上下文。算法细节以 `docs/references/routing-algorithms.md` 为准，
> 实验结论以各结果文档为准；本文与它们不一致时，以它们为准，并请在交付物里指出矛盾（见第 8 节）。
>
> **0.2.1 勘误（WHI-1547）**：§5.1、§5.2、§6.1、§6.6 的措辞按
> `docs/references/research-021/sources.md` §4 的 D1–D3 做了限定；数字、出处和冻结证据都没有改。
> 比较域、证书、计时和工作量单位的定义见 `docs/references/research-021/contract.md`（D4、D5）。

**全文适用范围**：除非另有说明，本文引用的所有质量和工作量结论都只适用于以下条件：

- 冻结语料 `mantle-5src-101082044`：区块 101082044，`bundle_hash` `717c21f3…3143`，143 个池，
  398 个 case（96 tuning / 302 report）；
- gross-only 目标函数；
- 对应文档登记的 profile 和预算。

这些结论不是全局最优性声明，不推广到其他区块或其他链，也不是生产延迟声明。所有计时都来自
共享工作站，主机负载见各文档，只作诊断参考。

## 1. 你的任务

1. **评估**：找出每个策略在哪些图结构、金额或流动性分布下会失效、选到次优解或浪费报价。
2. **新方向**：提出仓库里还没有的路由算法方向。
3. **优化点**：提出现有策略可以改进的地方，包括质量（gross output）、报价次数、延迟和内存。

每条建议都要写清楚：

- 动机，也就是它针对哪个已知问题；
- 预期收益和代价；
- 适用范围和可能的退化情况；
- 如何在现有 benchmark 或测试 fixture 上验证，需要具体到命令和对比指标（见第 7 节）；
- 是否和已尝试或已否决的方案重复（见第 5 节）。

**不要做的事**：

- 不要修改运行时代码、默认 profile 或默认策略。本阶段只做分析和提案。
- 如果需要做原型验证，放在单独的分支和 worktree 上，注明是实验，并遵守
  `docs/GIT_WORKFLOW.md`。不要在主 clone 里改动。
- 不要在 report split 上调参。调参只用 tuning split，report split 只用于一次性的 held-out
  判定（`docs/DESIGN.md` §2.4；v1 验收 §3 和 WHI-1449 都遵守这条纪律）。
- 不要把 `metis_inspired` 写成或当成 Jupiter Metis。

## 2. 必读文档（按顺序）

| 顺序 | 文件 | 看什么 |
|---|---|---|
| 1 | `docs/references/routing-algorithms.md` | 9 个策略的原理和手算示例（§2–§10）；§11 真实区块走查；§12 对比矩阵、复杂度和源码阅读图；§13 运行边界和已知债务 |
| 2 | `docs/references/strategy-groups.md` | 基础组、优化组和实验组（`custom`）的划分，`--strategies` 的四种模式，以及 `uni_sor_fast` 的定位 |
| 3 | `docs/references/v1-acceptance.md` | **目前唯一的全语料 held-out 报告**（6 个基础算法，0.1.0）：§3 调参（跳数、chunks、网格步长），§5 结果，§10 局限 |
| 4 | `docs/references/latency-optimization-results.md` | L08 最终测量：L02–L07 的判定（§3.3、§4），包括未采用的方案 |
| 5 | `docs/references/latency-optimization-research.md` | 热点归因（§3），实验规划（§4），以及**故意不优先做的方向**（§5） |
| 6 | `docs/references/jupiter-metis-challenge.md` | §8 考虑过并否决的候选方案；§9.2 `metis_inspired` 的契约；§9.3 与枚举产生分歧的几类原因 |
| 7 | `docs/references/metis-challenge-results.md` | WHI-1449 各 arm（A0、M3、M4、M4-off）的冻结结果和 keep 判定；§6 这些证据**不能**说明什么 |
| 8 | `docs/DEFERRED_ISSUES.md` | 已知、已接受的债务，包括 SOR 的 token 环和拆单成本未标定 |
| 9 | `docs/DESIGN.md` §2.8 | 已登记但未实现的研究方向（Balancer、Tines、CFMM 等） |
| 10 | `docs/references/single-request.md` | 单请求 CLI（`main.py quote`）的用法 |

各实验的细节文档：`latency-baseline.md`（L01），`latency-l02-empty-cl-spans.md`，
`latency-l03-math-reuse.md`，`latency-l04-prefix-reuse.md`，`latency-l05-graph-reuse.md`，
`latency-l06-sor-shortlist.md`，`latency-l07-adaptive-sampling.md`，以及
`colleague-routing-contract.md`（同事设计，见第 5 节）。

**源码入口**在 `routing/algorithms/`：

- 6 个基础策略和 `metis_inspired` 各有一个模块；
- `uni_sor_adaptive` 和 `uni_sor_optimized` 共用适配层 `uni_sor_strategies.py`，其引擎是
  `uni_sor_fast.py`，核心是 `uni_sor_port.py` 的翻译代码；
- 注册表在 `routing/algorithms/registry.py`（`ALGORITHMS`、`BASE_STRATEGIES`、`OPTIMIZED_STRATEGIES`）；
- `--strategies` 的组装逻辑在 `benchmark/strategies.py`，其中 `metis_inspired` 在 `all` 模式下
  由这里追加；
- 独立回放由 `routing/evaluator.py` 完成；
- 精确报价控制（L02–L04）的安装器是 `pools/exact_controls.py`。

## 3. 9 个策略速览

内容与 `routing-algorithms.md` §12.1 一致：

| 策略 | 组别 | 多跳 | 拆单 | 共享中间池 | 协议 | 搜索机制 | 最优性 |
|---|---|---|---|---|---|---|---|
| `direct` | 基础 | 否 | 否 | – | 全部 5 种 | 穷举直连池 | 已评估中最优 |
| `single_path` | 基础 | ≤H | 否 | – | 全部 5 种 | 按跳数优先的有界 DFS | 已评估中最优 |
| `direct_split` | 基础 | 否 | ≤S | 否 | 全部 5 种 | 精确网格 DP | 网格上最优（加性 gross 下） |
| `path_split` | 基础 | ≤H | ≤S | 否 | 全部 5 种 | 背包式分支限界 | 池不相交路径、网格上最优 |
| `incremental_graph` | 基础 | ≤H | ≤K 块 | **是** | 全部 5 种 | 贪心边际分块 | 局部启发式 |
| `uni_sor_port` | 基础（Uniswap SOR 等价性参照） | ≤H | ≤S | 否 | 仅 CPMM + CL（不含 LB） | FIFO 分层优先队列 | 局部启发式 |
| `uni_sor_adaptive` | 优化，实验性，需显式选用 | ≤H | ≤S | 否 | 仅 CPMM + CL | 粗到细采样报价表 + 原样 SOR 核心 | 局部不动点，近似 `uni_sor_port` |
| `uni_sor_optimized` | 优化，实验性，需显式选用 | ≤H | ≤S | 否 | 仅 CPMM + CL | 5%/100% 路由短名单 + 同样的采样；报价层使用精确控制 L02–L04 | 短名单内的局部不动点，近似 `uni_sor_port` |
| `metis_inspired` | 实验（`custom` 组），需显式选用 | 每块 ≤H_L | ≤K 块 | **是** | 全部 5 种 | 贪心分块，每块做分层 label 搜索 | 局部启发式；每个（层, token）只保留一个 label |

说明：

- `metis_inspired` **不是** Jupiter Metis。Metis 的路由源码没有公开，所有 label 规则都是本仓库
  自己推断的（`jupiter-metis-challenge.md` §9.2 中标注为 *[inferred]*）。
- **搜索深度不同**：`metis_inspired` 每块的搜索深度是 `graph.label_hops`，其他策略共用
  `search.max_hops`。
  - 在 `--strategies all` 下，如果源 profile 没有声明 `label_hops`，就取已登记 arm
    `config/metis_challenge/m4.yaml` 的值 4（`benchmark/strategies.py`）。
  - `config/daily_gross.yaml` 的 `search.max_hops` 是 2，`config/full_gross.yaml` 是 3。
  - 所以 `all` 模式下这一行和其他行不在同一个搜索空间里，CLI 和报告会打印这一差异。
- `uni_sor_fast` 不在这 9 个策略里。它是由 profile 显式选择的独立实验，两个优化策略只是它的
  两份冻结配方（L08 arm H3 和 H4）。
- `uni_sor_port` 保持和上游 Uniswap SOR 选择逻辑一致（parity），所以它的已知缺陷（见第 5 节）
  是有意保留的。

## 4. 评价框架与约束

- **目标函数**：
  - 本文引用的结论基本都是 **gross-only**，只看总输出，不扣 gas 或其他执行成本。
  - 仓库另有 `empirical_cost`（net）目标函数，用的是成本模型 v1（`config/daily.yaml`、
    `config/full.yaml`，`cost-model.md`）。但拆单和共享池计划的成本**没有标定**，这类计划在
    net 下是 unranked（`v1-acceptance.md` §5.4、`DEFERRED_ISSUES.md`）。
  - `main.py quote` 拒绝 `empirical_cost` profile（`routing-algorithms.md` §13）。
- **数据**：
  - 全语料：冻结语料 `mantle-5src-101082044`，含 tuning / report 两个 split，以及 matched
    V2/V3 cohort（`sor_cohort*`，与 SOR 的 98 个池同等能力）。见 `v1-acceptance.md` §2。
  - 延迟实验：用 L01 的 24 个 case 矩阵加 sentinel（`latency-baseline.md` §3）。
  - §11 单请求走查：区块 101082044 的 19 池 fixture（`tests/fixtures/corpus/bundle`），
    profile 为 `config/daily_gross.yaml`。
- **硬约束**：
  - 报价必须用精确的整数协议数学；
  - 结果必须确定：v1 做过固定顺序、反向顺序、打乱顺序和重跑的对照，均 0 差异
    （`v1-acceptance.md` §6）；
  - 最终计划必须能独立回放验证，回放不通过就判为 `invalid_plan`；
  - 预算由 `time_limit_seconds`、`max_quotes` 和 `max_candidates` 控制。运行器在硬时间或报价上限
    处截断时判为 `timeout`，被截断的搜索永远不能判为 `no_route`（`routing/algorithms/base.py`
    的 `SolveStatus`）。
- **协议**：共 5 种来源：Agni V3、FusionX V3、Uniswap V3、Merchant Moe Classic、Merchant Moe LB。
  SOR 系列不支持 LB（Liquidity Book），这是契约偏差 D-4。
- **§11 真实案例的局限**：在这个 fixture 里，USDC 只有 4 个直连 USDT0 的池，所以多数策略结果
  几乎一样。已知结果（`routing-algorithms.md` §11.1）：
  - `incremental_graph` 和 `metis_inspired`：10000663447，264 次报价；
  - `path_split`：10000660449，80 次报价；
  - `uni_sor_optimized`：10000660449，12 次报价。

  **判断优化空间请看全语料 benchmark，不要只看这一个例子。**
- **全语料报告的现状**：
  - 最新的全语料 held-out 报告是 v1 验收（WHI-1447，0.1.0），**只含 6 个基础算法**：
    `docs/references/v1-acceptance/report-full/` 和 `report-daily/`，各含 `report.html` 和 CSV。
  - 两个优化策略**只**在 L08 的 24 个 case 矩阵上测过，而且只和 `uni_sor_port` 做了对比。
  - `metis_inspired` 只在 WHI-1449 的 A0 / M3 / M4 / M4-off 这几个 arm 里测过，其中 held-out
    只跑了 A0 对 M4。
  - **目前没有包含全部 9 个策略的全语料报告。** 主 clone 的 `data/results/` 里有一个 8 策略的
    运行 `20260928T091414798866Z-cc8dd15e`，但它已中断（`state: interrupted`，1146 个
    `cancelled`），不是结果。
  - 如果你需要 9 个策略的全语料对比，这本身就是第一个实验，命令见第 7 节。

## 5. 已知问题与已尝试方案

### 5.1 已知局限

详见 `routing-algorithms.md` §13 和各章的 §x.9。

- **跳数上限是最大的质量杠杆，而深搜很贵**：
  - 在 tuning split 上，2 跳的拆单搜索比 3 跳的 best known 平均低 13–15 bps（p95 约 46–50 bps），
    中位耗时约为后者的 1/25–1/30（`v1-acceptance.md` §3.2）。
  - 在 held-out 上，daily profile（2 跳）的 p95 比 3 跳少约 40–65 bps（`v1-acceptance.md` §5.3a）。
- **`incremental_graph` 的质量对 `graph.chunks` 不单调**：
  - 在 3 跳下，`chunks 200` 在 79/96 个 case 上达到 best known，但它的 p95 和 max 缺口最大
    （`v1-acceptance.md` §3.2）。
  - 在 held-out 上，3 跳的 `incremental_graph` 不如 2 跳的 case 有 34 个（五源）和 43 个（matched
    cohort）（`v1-acceptance.md` §5.3a）。
  - 原因是贪心分块会被锁进较差的结构。
- **`uni_sor_port` 在 3 跳下会选出构成 token 环的路线组合**：held-out 上有 12 个 `invalid_plan`。
  为保持和上游的 parity 没有修复（`DEFERRED_ISSUES.md`，`v1-acceptance.md` §9）。
- **`uni_sor_port` 对 gas 不感知**：它的 gas 分数为零（A-3），即使在 `empirical_cost` 下也按原始
  报价选择。两个优化策略继承了这一点。
- **SOR 系列不覆盖 LB 池**：在五源全语料上，`path_split` 相对 `uni_sor_port` 的覆盖差距 p95 为
  +31.31 bps，这是能力差异，不是搜索质量差异（`v1-acceptance.md` §5.3）。
- **拆单和共享池计划在 net 下没有成本**：`empirical_cost` 下，拆单搜索会退回到可排名的单路线计划，
  在改变了计划的 case 里中位放弃 14–22 bps gross（`v1-acceptance.md` §5.4）。
- **`search.max_splits` 从来没有扫过**，4 是 DESIGN 的试验值，未经验证（`v1-acceptance.md` §3.3）。
- **`metis_inspired` 每个（层, token）只保留一个 label，会丢掉合法前缀**：在构造的 fixture X4 上，
  最优 4 跳路径 S–A–X–Y–D 本身不重复任何 token；但 X 处金额占优的 2 跳 label S–Y–X 已经访问过 Y，
  不能再接 X→Y，于是合法的较小前缀 S–A–X 被丢掉（`label_skipped_revisit`）。损失 4994.98 bps；
  这是有意接受、没有修复的局限（`routing-algorithms.md` §10.5 第 8 步）。
  `label_hops ≥ 4` 时还有 prefix-dependent admission 损失（fixture X4b：丢掉的后续 S–B–V–X–D 也合法，
  只有占优 label 的后续会与已提交的计划形成 token 环）。
  - 这两类都不是“最优路径本身含环”。真正重复 token 的候选在 v1 evaluator 下本来就不可行
    （`research-021/sources.md` D3，重算 R1、R7、R8）。
- **`metis_inspired` 在小图上不省工作量**：
  - 教学图第 1 块用了 10 次 relaxation，逐条枚举是 6 条路径（§10.5 第 4 步）。
  - 只有每跳有多个并行池时才明显省：fixture X1 上是 100 次 relaxation 对比 280 条路径，848 次报价
    对比 1011 次（§10.5 第 6 步）。
- **两个优化策略都是局部近似**：
  - `uni_sor_adaptive` 在构造的窄最优例子上损失 26.67 bps（§8.5 第 8 步）。
  - `uni_sor_optimized` 的短名单会漏掉组合里有用、但在两个探测金额下单独排名都不在前 8 的路线
    （§9.9）。
- **能利用共享中间池的只有 `incremental_graph` 和 `metis_inspired`**，两者都是贪心分块，没有全局
  最优保证。

### 5.2 已尝试的方案

结论一栏的含义：

- **keep**：保留为可选的实验策略或结论成立；
- **默认关闭**：代码已合入、精确，但未被采用；
- **reject**：已否决或不再继续；
- **未做**：考虑过但有意没有实施。

L08 的 7 个预登记**决策**比较（L02、L03、L04、L05、L06、L07-combined、L07-adaptive-only）都是
`inconclusive`，唯一原因是主机负载超过 L01 的 5.0 规则。5 个信息性比较里只有 L07-sampling-ablation
（H1→H2，两边都未超负载）是干净的，判定 `opt_in_only`；其余 4 个同样因负载 inconclusive
（`latency-optimization-results.md` §3.3；`research-021/sources.md` D2）。这**不是**“更慢”的
证据，只是没有可用于采用决策的干净计时。

**A. 延迟优化 L01–L08（0.1.2，WHI-1503–1510）**

范围：L01 矩阵（24 case + sentinel）、`daily_gross`、区块 101082044。L02–L05 是精确控制，作用于
共享报价层或图搜索，不只针对 SOR；L06–L07 是 SOR 专用的启发式。

| 编号 | 做了什么 | 结论 | 原因或关键数字 | 出处 |
|---|---|---|---|---|
| L01 | 可复现的延迟和质量基线协议、驱动与比较器 | keep（测量基础） | 基线实验 `d5061563` 的负载最高达 161.4，只作语义参照，不作计时证据 | `latency-baseline.md` §8 |
| L02 | 在零流动性、未初始化的 CL 区间整字跳过（`skip_empty_spans`） | 默认关闭（未采用：inconclusive） | 精确；300 条记录中实际执行的 `computeSwapStep` 为 6.26 M / 36.00 M 逻辑步（−82.6 %） | `latency-l02-empty-cl-spans.md` §1；`latency-optimization-results.md` §4 |
| L02 附带 | 非零 bitmap 字索引 | 未做 | 只在 L02 被采用后才有意义 | `latency-optimization-results.md` §4 |
| L03 | tick / bin 价格数学的有界精确 memo（每次求解 16384 / 4096） | 默认关闭（未采用：inconclusive，E2 负载达 34.99） | 精确；命中率为 tick 94.8 %、bin 94.6 % | `latency-l03-math-reuse.md` §1、§5 |
| L03 附带 | LB 排序 bin 树复用 | 未做 | 只占 0.2–0.7 % 的插桩求解时间，且需要状态身份键和失效机制 | `latency-l03-math-reuse.md` §6 |
| L04 | 跨金额复用 CL 已完成遍历前缀（`CLPrefixReuse` 4096 / 262144） | 默认关闭（未采用：inconclusive） | 精确；300 条记录中执行的 `computeSwapStep` 从 6,260,016 降到 732,770；对 `direct` 纯属开销 | `latency-l04-prefix-reuse.md` §1、§5 |
| L04 附带 | 只输出金额的接口；跨交换的前缀复用 | 未做 | 前者不需要（复用路径已返回完整状态），后者按设计不在范围内 | `latency-l04-prefix-reuse.md` §1 |
| L05 | `incremental_graph` 的边际分数和环检测复用（`graph_reuse=True`） | 默认关闭（未采用：inconclusive，E3/E4 负载 5.62） | 精确；50 条记录中 `marginal` 重算从 1,536,836 降到 202,209；在全部结转（carry）的 case 上纯属开销 | `latency-l05-graph-reuse.md` §1、§5 |
| L05 附带 | 堆 / argmax 索引跳过逐路径循环 | 未做 | 稳定的先到先得平局、每块 `max_candidates` 截断和失败计数都由有序循环定义 | `latency-l05-graph-reuse.md` §1 |
| L06 | `uni_sor_fast` 路由短名单，提名 `p5.100-k8-d0`（arm H1） | keep（实验，需显式选用；L06 比较 inconclusive） | tuning 上 0 损失，但 held-out 上 2 个 case（每个 cohort）损失 ≤ 1.125 bps；报价为参照的 0.218× | `latency-l06-sor-shortlist.md` §4.2、§5；`latency-optimization-results.md` §3.5 |
| L06 激进设置 | `p100-k2-d0`（只在 100 % 探测） | reject（不继续） | held-out 最多损失 35.0 bps；只探测 100 % 的设置在 tuning 上也会损失 25–27 bps | `latency-l06-sor-shortlist.md` §4.1–§4.2 |
| L06 附带 | 上游式按 TVL / base token 的候选池预算 | 未做 | 冻结 bundle 没有统一单位的 TVL；按 TVL 或全额报价排名会漏掉有利的小额拆分 | `latency-l06-sor-shortlist.md` §1、§5 |
| L07 combined | 短名单 + 粗到细采样 `c25-r1-snone`（arm H2） | keep（实验，需显式选用；比较 inconclusive） | held-out 上相对 L06 没有额外损失，报价为参照的 0.152×；H1→H2 是 L08 唯一的干净比较，判定 `opt_in_only`（快 17.0 % / 16.7 %，0 额外损失） | `latency-l07-adaptive-sampling.md` §4.2；`latency-optimization-results.md` §3.3 |
| L07 adaptive-only | 近乎全量候选 + 采样 `c25-r1-snone`（arm H3，即现在的 **`uni_sor_adaptive`**） | keep（实验，需显式选用；比较 inconclusive） | L08 held-out 上 0 损失，报价为参照的 0.467×；窄最优测试表明它仍可能漏解 | `latency-optimization-results.md` §3.5、§4 |
| L07 激进设置 | `c50-r1-s250`、soft cap、50 % 粗网格 | reject（不继续） | 损失最多 35.7 bps；soft cap 是否可接受由 owner 决定 | `latency-l07-adaptive-sampling.md` §4.1–§4.2；`latency-optimization-results.md` §4 |
| H4 组合 | H2 + L02–L04（即现在的 **`uni_sor_optimized`**） | keep（实验，需显式选用；计时 inconclusive） | H2→H4 精确，0 语义差异；报价为参照的 0.152×；held-out 损失与 H2 相同（2 个 case，最大 1.125 bps） | `latency-optimization-results.md` §3.2、§3.5、§4 |
| L08 | 预登记的 10 个 arm 最终测量（R、E1–E4、S0、H1–H4） | 报告被 owner 接受；**没有任何方案被采用** | 10 个 arm 中 8 个超过负载规则；任何默认启用都需要一次安静主机上的新测量和 owner 对损失的明确接受 | `latency-optimization-results.md` §3.1、§5 |

**B. Metis 挑战（0.2.0，WHI-1448 研究 + WHI-1449 实现）**

范围：tuning split（96）和 report split（302），五源全语料，`full_gross` 的值（3 跳、4 拆、5 %、
50 块、900 s / 300,000 次报价），gross-only。**这不是 Jupiter Metis。**

| 编号 | 做了什么 | 结论 | 原因或关键数字 | 出处 |
|---|---|---|---|---|
| WHI-1448 候选 | Brent 法拆单优化（C9） | reject | 来源没有说明优化什么变量、在哪些路线上、受什么约束；实现它等于自己发明方法 | `jupiter-metis-challenge.md` §8 |
| WHI-1448 候选 | 动态选前 3 个中间 token | reject | Mantle 的 8 token 宇宙已经暴露了所有中间 token | `jupiter-metis-challenge.md` §8 |
| WHI-1448 候选 | 每笔交易接入更多 DEX；实时并行刷新报价；JIT / prop-AMM / RFQ 聚合；以 Jupiter 实时报价为参照 | reject | 分别因为：Mantle 上没有账户锁的对应物；属于基础设施且与冻结状态设计冲突；不在已准入的 Mantle 域内；DESIGN §2.8 明确不用 | `jupiter-metis-challenge.md` §8 |
| H-M1 / S1 | 分层、报价驱动的每块 label 搜索，fixture X1–X7、X4b | pass | 全部 fixture 通过 | `metis-challenge-results.md` §1–§2 |
| A0 vs M3 / S2 | 3 跳下 label 搜索与穷举枚举是否一致（tuning） | pass | 89 个 case 完全一致，7 个可归因；4,800 个块中 4,588 一致、212 平局、0 无法解释 | `metis-challenge-results.md` §3 |
| A0 vs M3 / S3 | 工作量 | pass | 中位数 A0 `paths_scored` 218,591 对比 M3 `label_relaxations` 9,229.5（单位不同）；M3 在 96/96 个 case 上更少；报价中位 17,491.5 对比 27,175.5 | `metis-challenge-results.md` §3 |
| M4 vs M4-off / S4 | 4 跳 label 搜索与 4 跳穷举枚举（tuning） | 只报告；**不能声称剪枝带来可行性** | 两者都 96/96 `ok`，都没超上限；gross 在 12/96 个 case 上不同；M4-off 报价中位 116,745，最长求解 270.9 s | `metis-challenge-results.md` §4 |
| H4 诊断 | 对上述 12 个差异 case 做逐块归因 | pass | 一致 398、平局 174、token_revisit 22、prefix_admission 6，0 无法解释 | `metis-challenge-results.md` §4 |
| A0 vs M4 / H-M1b | held-out（report split，只跑一次）：M4（label，4 跳）对比 A0（`incremental_graph`，3 跳） | **keep（实验，需显式选用；不是默认）** | 301 对 `ok` 中 132 胜 / 48 负 / 121 平，精确双侧 p = 2.93 × 10⁻¹⁰；平均 +1.41 bps，中位 0 bps；131/132 胜利来自含 4 跳块的计划 | `metis-challenge-results.md` §1、§5 |
| H-M1b 的限制 | 这次比较**同时**改变了机制和跳数上限 | 未分离 | M4-off 没有在 report split 上运行（不在登记范围内），所以收益不能单独归因于 label 机制；也没有 net 结果和 matched cohort 视图 | `metis-challenge-results.md` §6 |

**C. v1 验收的调参（0.1.0，WHI-1447，只用 tuning split）**

| 编号 | 做了什么 | 结论 | 原因或关键数字 | 出处 |
|---|---|---|---|---|
| 跳数 | 2 跳 vs 3 跳 | keep：full 用 3，daily 用 2（声明的简化） | 2 跳拆单搜索平均比 3 跳 best known 低 13–15 bps | `v1-acceptance.md` §3.2、§3.3 |
| chunks | `incremental_graph` 的 `graph.chunks` 扫描 | keep：daily 200，full 50 | 2 跳下缺口随 chunks 单调下降；3 跳下不单调，50 的平均和 p95 缺口最低 | `v1-acceptance.md` §3.2 |
| 网格步长 | `percent_step` 2 / 4 / 5 / 10 | keep 5 | 2 % 网格使 `path_split` 平均多 ~0.35 bps，但中位耗时 ~2.7×；10 % 平均少 ~0.36 bps | `v1-acceptance.md` §3.2、§3.3 |
| 拆单数 | `search.max_splits` | 未做（未经验证） | 保留 DESIGN 试验值 4，没有扫描 | `v1-acceptance.md` §3.3 |

**D. 同事设计（0.2.0，WHI-1536 研究 + WHI-1537 实现）**

| 编号 | 做了什么 | 结论 | 原因或关键数字 | 出处 |
|---|---|---|---|---|
| 固定计划回放 | 把同事 M3 格式、只含 SWAP 的计划转成 `RoutePlan` 并独立回放（`routing/colleague_plan.py`） | keep（适配器，有边界） | 只能测语义等价和适配开销，**不是**路由质量比较 | `colleague-routing-contract.md` §0、§5、§10 |
| JIT 候选策略 | baseline + JIT 候选的执行时选择 | reject（不支持） | 阈值算术、零 baseline 和候选失败处理都没有定义（D-P1–D-P4），也没有冻结的 JIT/RFQ 提供方 | `colleague-routing-contract.md` §6 |
| 同事的寻路算法 | 按同事设计实现求解器 | 受阻（blocked） | 缺 11 项输入（S1–S11），包括模板定义、候选生成和目标函数；不能从编码示例推断 | `colleague-routing-contract.md` §4 |

**提案前请先核对以上清单，避免重复已否决或已测过的方案。**

## 6. 可选的起点假设（未验证，仅供参考）

以下每条都已和第 5 节对照过。这些都**不是**已测得的收益。

1. **修复 token 重复经过的损失**：每个 token 保留多个 label（k-best），或加入路径签名。
   - 已有背景：WHI-1448 **有意不修复**这一点，因为 k-best 是没有来源依据的额外参数
     （`jupiter-metis-challenge.md` §9.3）。它没有被测过，也没有被否决。
   - 代价：relaxation 次数增加。
   - 注意：只按金额保留前 k 个 label 不是一般性的修复，金额更高的几个 label 可能访问过同一批 token
     （外部报告 §2.2，本仓库未复现；安全的支配规则由 WHI-1549 研究）。
   - 验证：fixture X4 / X4b，以及 H4 诊断里的 22 个 token_revisit 块和 6 个 prefix_admission 块
     （`metis-challenge-results.md` §4）。
2. **把机制和跳数的效应分开**：在 report split 上补跑 M4-off（4 跳枚举），或跑一个同为 4 跳的
   对照，从而单独衡量 label 机制的贡献。
   - 出处：`metis-challenge-results.md` §6。
   - 这需要一份新的预登记计划，不能改写已冻结的 WHI-1449 campaign。
3. **更便宜地搜得更深**：跳数是已测得的最大杠杆（第 5.1 节）。`metis_inspired` 是一次尝试，但
   held-out 效应很小（平均 +1.41 bps）。其他思路可参考 DESIGN §2.8：Sushi Tines 的累积流路由，或
   分段线性优化。
4. **边际价格均衡式拆单**：让各条路径的边际价格相等，按注水法或凸优化思路分配金额，作为网格 DP
   和贪心分块之外的另一种方案。
   - 已有背景：DESIGN §2.8 已把 Balancer 式连续边际分配和 CFMM / 对偶优化登记为未来方向；
     `latency-optimization-research.md` §5 把它列为“故意不优先”，理由是它要处理迭代和局部极小，
     而且没有证据适用于本仓库的整数 CL / LB / 共享池域；WHI-1448 否决的 Brent 法（C9）是同一
     类方法，但否决原因是来源不足，不是方法本身不可行。
   - 如果要提，必须给出整数计划的恢复方法，并在相同目标函数下与 `direct_split`、`path_split`、
     `incremental_graph` 比较（DESIGN §2.8 的要求）。
5. **更好的初始分配或候选排序**：先用小额报价估算，再做精确细化，减少精确报价次数。
   - **L06 已部分做过**：它用 5 % 和 100 % 两个金额的精确报价给路线排序。只看 100 % 的排名在
     tuning 上会损失 25–27 bps。
   - **按 spot price 或 TVL 排名没有做**：冻结 bundle 没有 TVL，而且这种排名会漏掉小额拆分
     （`latency-l06-sor-shortlist.md` §1）。
   - 新提案应该说明它和 L06 / L07 的区别，比如作用于 `incremental_graph` 或 `path_split`，而
     不是 SOR。
6. **SOR 系列支持 LB**：扩大覆盖的协议。
   - 这没有被尝试过。但 `uni_sor_port` 的 LB 缺失是契约偏差 D-4，给它加 LB 会破坏和上游的
     parity。只能在一个新命名的变体里做，类似 `uni_sor_fast`。
   - 历史覆盖差距（只是动机，不是上限）：五源全语料上与 `path_split` 的覆盖差距 p95 为 +31.31 bps
     （`v1-acceptance.md` §5.3）。p95 不是最大值（同一行的均值为 +1598.63 bps），而且这是两个覆盖
     范围不同的策略之间的差距，不是给固定策略加 LB 后可达的收益或收益上界
     （`research-021/sources.md` D1）。
7. **修复 SOR 的 token 环**：用一个有文档记录的适配偏差，跳过会形成环的路线组合
   （`DEFERRED_ISSUES.md` 给出的修法）。可以放在新变体里；held-out 上有 12 个 case 可用来验证。
8. **扣成本的目标函数**：
   - net 目标函数**已经存在**（`empirical_cost`，成本模型 v1），不是新方向。
   - 真正的缺口是：拆单和共享池计划的成本没有标定；SOR 在选择时不用成本（A-3）。
   - 这两点都记在 `DEFERRED_ISSUES.md` 里，需要新的成本数据（更长或更宽的窗口），会改变评价
     标准，需要单独立项。
9. **缓解 `incremental_graph` 的贪心陷阱**：3 跳下质量对 chunks 不单调（第 5.1 节）。可以考虑
   多起点、回退已提交的块，或对最终计划做局部修正。这没有被尝试过。
10. **扫描 `search.max_splits`**：它从来没有被验证过（`v1-acceptance.md` §3.3）。成本低，只需要
    在 tuning split 上跑一次扫描。
11. **测量本身**：L02–L05 都是精确的，但都因为主机负载而 inconclusive。任何“更快”的提案都应该
    先说明如何在安静主机上满足 L01 规则，例如复跑冻结的 L08 v1 session
    （`latency-optimization-results.md` §5）。

## 7. 可复现验证命令

**基础检查**（在任意 worktree 中）：

    uv run python docs/examples/routing-algorithms/run_examples.py   # 11 个示例套件
    uv run pytest tests/docs -q                                       # 文档示例的数字断言
    uv run pytest -q                                                  # 完整测试（本文核对时：2295 passed，6 skipped）
    uv run ruff check . && uv run mypy

**单请求**（`single-request.md`）：

    uv run python main.py quote --bundle tests/fixtures/corpus/bundle \
      --profile config/daily_gross.yaml --token-in USDC --token-out USDT0 --amount 10000 --details

`--strategies all`（默认）会运行 9 个策略；`base` 只跑 6 个基础策略；`optimized` 只跑两个优化策略；
`profile` 严格按 profile 列出的算法运行，用于回放。

**全语料 benchmark**（入口是 `main.py run`，报告由 `main.py report` 生成；完整流程见
`v1-acceptance.md` §11）：

    # 冻结语料在主 clone 的 gitignored 目录里；重建方法见 docs/references/corpus.md §8
    C=/path/to/router-algorithms-optimizer/data/corpus/mantle-5src-101082044
    uv run python main.py validate --bundle $C/bundle        # bundle_hash 应为 717c21f3…

    # 探索和调参：只用 tuning split（96 个 case）
    uv run python main.py run --bundle $C/bundle_tuning --profile config/daily_gross.yaml

    # held-out 判定：report split（302 个 case），每个预登记结论只跑一次；
    # 与 SOR 同能力的对比用 matched cohort：$C/sor_cohort_report
    timeout 43200 uv run python main.py run --bundle $C/bundle_report --profile config/full_gross.yaml

    uv run python main.py report data/results/<run id> [更多 run ...] --output data/reports/<名称>
    uv run python main.py order-check data/results/<run A> data/results/<run B>   # 确定性检查

注意：

- `main.py run` 默认也是 `--strategies all`。在 `full_gross.yaml` 上它会运行 9 个策略，其中
  `metis_inspired` 使用 profile 自己的 `chunks: 50`，加上 M4 的 `label_hops: 4`、`label_pruning: true`。
  要复现 v1 的 6 算法报告，请用 `--strategies base`。
- 耗时量级：v1 的 full profile 6 算法 held-out 运行在并发负载下约 4.5–5 小时；daily profile 约
  1–1.7 小时，不含内存轮（`v1-acceptance.md` §7）。9 策略运行还没有测过。请在后台带超时和日志运行。
- 每个 run 目录都有 `manifest.json`，其中的 `replay_command` 可以原样复现该 run。

**已有报告的位置**：

- 全语料 6 算法（held-out）：`docs/references/v1-acceptance/report-full/report.html`、
  `report-daily/report.html`，以及同目录下的 CSV（`paired_gross.csv`、`status_counts.csv` 等）。
- L08 延迟和启发式质量：`docs/references/latency-results/final-dbac5a10.md`。
- Metis held-out 逐 case 结果：`docs/references/metis-challenge-results/heldout-per-case.csv` 和
  `summary.json`。原始证据在仓库外（`metis-challenge-results.md` §8）。

## 8. 交付物格式

请交付一份 Markdown 报告，包含：

1. **现状评估**：每个策略 1–3 条弱点，并附证据，引用文档章节、源码行号或 fixture。
2. **新算法方向**：每条都按第 1 节要求的字段写。
3. **现有策略的优化点**：同样按第 1 节的字段写。
4. **优先级**：按性价比排序，并给出建议的首个实验，包括它用哪个 split、哪个 profile、对比哪个
   基线、看哪些指标。
5. **文档问题**：你认为本文档或现有文档里缺失、过时或自相矛盾的地方。

每个数字都要注明出处和适用范围（语料、split、profile、目标函数）。
