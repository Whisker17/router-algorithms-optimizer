# 路由算法探索：第一轮续研报告

**日期：2026-09-29**  
**定位：分析、可复核的独立数学验证与下一阶段实验提案；不是运行时代码变更。**

## 0. 结论与证据边界

本轮建议把主线从“再增加一个经验参数”转向**保留正确的搜索状态，并为近似搜索建立可检查的上界**：

1. 优先研究 **history/admission-aware labels**：先区分“前缀被错误合并”与“某类路径根本不在可行域内”，再决定状态签名。只增大按金额排序的 k，不能提供一般正确性保证。
2. 并行建立 **certified integer allocation** 小型参照器：连续模型只负责构造安全上界，实际候选及最终输出使用整数协议数学。不要把连续凹性当作整数边际递减。
3. 对共享池的贪心问题，研究 **完整状态回滚后的有限后缀重优化**，而非在冻结初始报价上交换几个路径权重。

已完成：读取用户提供的 handoff；检索协议源码和优化文献；构造并运行独立反例及随机性质检查；输出脚本、逐例结果和本报告。

**未完成且不声称完成：**目标仓库源码审计、必读参考文档的逐项交叉核对、仓库 pytest/ruff/mypy、X4/X4b 的实际复跑、143 池冻结语料实验、九策略全语料排名、生产延迟或净收益测量。

### 0.1 为什么必须保留这个边界

公开 handoff 成功读取，但本环境的 `git clone` 因无法解析 GitHub 主机名失败；若干关键源码、参考文档的网页及原始文件入口也未成功返回内容。已查找并提供 GitHub 连接入口，未把它当作已连接。

因此：

- 仓库现状一律标作“handoff 所述”，不伪造源码行号。
- handoff 声明的源码基线是 `358bbe0c0a86965607c950d12ee0ce0e84915b2d`；**本轮未独立确认当前 dev HEAD 或 handoff 自身提交号**。
- 这里的 Python 文件是独立数学验证器，不是 `metis_inspired`、`direct_split` 等仓库实现的复制或替代。它没有注册策略、修改 profile、创建目标仓库分支或提交。
- 未取得 `docs/GIT_WORKFLOW.md` 正文，故不声称完成了该工作流要求。后续仓库原型必须在读取该文件后放入独立分支及 worktree。
- 未读取或调参使用 report split 的逐 case 数据；合成测试不属于仓库 tuning/report 的任何一个 split。

仓库边界和任务约束来源：[H，§1、§4、§7、§8]。下文对仓库内部文档章节的提及，未特别注明时均是 **handoff 的导航引用**，不是已直接验证原文。

---

## 1. 现状评估：九个策略

下表将 handoff 的可行域描述与本轮结构性推论分开。没有新增真实语料收益数字。

| 策略 | 1–3 个主要弱点 | 证据层级与验证入口 |
|---|---|---|
| `direct` | 不能表达多跳改善；不能表达多个直连池分流；平行池很多时仍要逐池判别。 | 前两点是 handoff §3 的可行域限制；第三点是由穷举结构推出。用多跳胜出、双池分流胜出的合成例分开验证。 |
| `single_path` | 单路径无法利用分流降低冲击；跳数和候选截断会改变可搜索空间；优先发现某条路径不等于它最优。 | handoff §3、§4；在同跳数、同候选集合下与无截断枚举比较，单列 budget 截断。 |
| `direct_split` | 不能表达中间 token；“最优”受金额网格及 `max_splits` 限定；网格误差不能用连续边际递减替代证明。 | handoff §3；本轮整数反例、两池整数/网格参照器。 |
| `path_split` | 池不相交限制排除共享中间池；路径数与兼容组合都可能膨胀；报价表完备不代表包含全部可行拓扑。 | handoff §3；本轮共享池反例。候选生成、报价、组合搜索应分别记账。 |
| `incremental_graph` | 当前块最优不保证整笔最优；增加 chunks 或深度可能改变早期承诺并恶化最终输出；共享状态使“撤掉一路”不能只减掉旧报价。 | handoff §5.1；第三点由状态更新推出，并由本轮共享池检查说明。 |
| `uni_sor_port` | 协议覆盖与其他算法不完全相同；局部选择与最终计划合法性可能不一致；上游 parity 限制了直接修复的空间。 | handoff §3、§5.1、§6；改进应另命名，不能把能力差异当作纯搜索优劣。 |
| `uni_sor_adaptive` | 局部细化停止不是全局未采样区间的质量证书；继承 SOR 的覆盖/选择边界；报价减少与净延迟改善是不同命题。 | handoff §3、§5.1–5.2；本轮提出显式上下界来区分“未发现改进”和“已排除改进”。 |
| `uni_sor_optimized` | 候选短名单可能永久排除组合中有用的路线；精确报价层不能修复候选遗漏；短名单误差与采样误差应分别消融。 | handoff §3、§5.1–5.2；在固定候选集合上测采样，再单独改变候选集合。 |
| `metis_inspired` | 每层/token 单 label 可能丢失不同的后续可行性；即使修复单块搜索，跨块仍是贪心；更深搜索和 label 机制的贡献不能混为一谈。 | handoff §3、§5.1、§5.2；本轮历史签名反例及 2×2 实验设计。这里不是 Jupiter Metis。 |

这九行是**条件性算法评价，不是新九策略排行榜**。尤其不能把本轮独立 `one` 搜索器的输出、报价数归到仓库 `metis_inspired` 名下。

---

## 2. 本轮实际验证的发现

所有金额均为 raw integer units。统一使用 Uniswap V2 风格的固定手续费因子 `997/1000`：

\[
q(x;R_i,R_o)=
\left\lfloor\frac{997xR_o}{1000R_i+997x}\right\rfloor.
\]

公式核对来源是官方 `UniswapV2Library.sol::getAmountOut` [U]。这不表示仓库五种来源都采用同一费率。零金额只表示不选择该分支，不执行零额 swap。

结果入口：[`results.json`](results.json)；可执行复现：[`verify_research.py`](verify_research.py)。

### 2.1 前缀合并错误，不等于最优路径本身含环

构造一个五 token、双向池图，输入 10,000，最多 4 跳。六个池如下，储备按表中方向排列：

| 池 | 方向 | 输入储备 | 输出储备 |
|---|---|---:|---:|
| ab | A→B | 1,000,000,000 | 1,000,000,000 |
| bc1 | B→C | 1,000,000,000 | 1,200,000,000 |
| ad | A→D | 1,000,000,000 | 990,000,000 |
| dc | D→C | 1,000,000,000 | 1,000,000,000 |
| cb2 | C→B | 1,000,000,000 | 2,000,000,000 |
| bt | B→T | 1,000,000,000 | 1,000,000,000 |

每个池也允许反向报价，且反向储备与正向一致。

| 独立搜索器 | 输出 | 路径 | 精确报价调用 |
|---|---:|---|---:|
| 每层/token 单 label | 9,938 | A→B→T | 7 |
| 每层/token/visited-set 一个 label | 19,560 | A→D→C→B→T | 10 |
| 所有简单路径穷举 | 19,560 | A→D→C→B→T | 12 |
| 允许重复 token、仍不重复池的有界 walk | 23,708 | A→B→C→B→T | 22 |

解释：

在 C，较大的金额可能来自已访问 B 的前缀；这个前缀不能再走 C→B。来自 D 的较小金额却可以接上 C→B→T。**被丢掉的最优简单路径没有重复 token。**

最后一行则属于另一个可行域：它确实重复 B。不能拿它的较高输出说明应该在现有 evaluator 下允许环；如果计划契约不允许这种环，它就是域外候选。

**对 handoff 的影响：**§6.1 应把“前缀 visited 历史导致的漏解”和“路径确实需要重复 token”分开描述。由于未取得 X4/X4b 正文，这里不判断它们分别属于哪一类。

### 2.2 单纯增大 k 不是一般正确性修复

在上图加入若干具有相同 visited 历史的平行优质前缀。对于按当前金额保留前 k 个 label 的规则，本轮分别构造了 k=1、2、4、8 的例子：

- beam 输出均为 9,938；
- 历史签名搜索均为 19,560。

这些例子说明：**金额排名上的多个 label 可以在“哪些 token 已经不能再用”这件事上完全冗余。**

这不是所有多 label 方法的反例。带资源支配、历史去重或完整状态的算法不同于这里的纯金额 top-k。一般的资源约束路径算法本就要求把正确的资源与支配规则编码进状态 [B]。

### 2.3 连续凹性不会自动变成整数边际递减

取储备 `(1000,1000)`，输入 x=0…6：

```text
q(x):       0, 0, 1, 2, 3, 4, 5
q(x+1)-q(x): 0, 1, 1, 1, 1, 1
```

第一次边际增量从 0 上升到 1，因此这个整数序列不满足离散凹性。

另一个双池例子：总输入 53，储备分别为 `(134,190)`、`(76,172)`。令

\[
G(x)=q_1(x)+q_2(53-x).
\]

结果 `G(0)=G(1)=G(2)=70`，但 `G(15)=76`。只接受严格正收益的 ±1 局部修正会在 x=1 的平台上停住。

**这不否定连续优化。**它否定的是未经证明就把“连续驻点/邻域没有改善”当作整数全局最优证书。连续模型作为上界或候选生成器仍然有价值 [C1, C2]。

### 2.4 共享池：静态相加、顺序回放、合并成交不是同一个问题

同一个 `(1000,1000)` 池，每次输入 100：

| 计算方式 | 总输出 |
|---|---:|
| 两次都对初始储备报价后相加 | 180 |
| 更新储备后顺序执行两次 | 165 |
| 合成一次输入 200 的交换 | 166 |

三者不相等。

因此，`sum(path_quote(initial_state, allocation))` 不能直接当作共享池计划的目标函数。也不能默认“把连续流量合并为一次 swap”与仓库分块执行语义等价。是否可合并、费用如何计提、整数取整发生在哪里，必须写入计划恢复契约。

### 2.5 多跳取整误差不能按“每跳少 1 个最终 token 单位”相加

先走 `(1000,1000)`，再走 `(10^9,10^11)`，输入 2：

- 逐跳整数执行最终输出为 99；
- 对两跳都保留连续金额时，最终输出约为 198.406；
- 第一跳丢掉的不足 1 个中间 token 单位，会被第二跳的兑换比例放大。

因此，“m 条路线、每条 h 跳，总误差至多 mh 个最终输出单位”不是普遍有效的恢复误差界。需要用后续最大兑换斜率传播误差，或者直接用精确回放和最终输出上界验证。

### 2.6 随机性质检查：通过，但只是合成域内证据

固定生成器、固定随机种子；没有使用 Mantle 数据。

| 检查 | 样本与域 | 结果 |
|---|---|---|
| 历史签名 vs 简单路径穷举 | 200 图，6 token，最多 4 跳，随机双向 CPMM 平行池 | gross 不一致 0；独立储备更新回放失败 0 |
| 输入顺序稳定性 | 上述 200 图各反向、打乱一次 | 400 次检查全部一致 |
| 单 label 对照 | 同一批图 | 7 图低于穷举；不是仓库策略测量 |
| 两池全整数拆单 | 200 组，总输入 50–1000 的范围内随机生成 | 与穷举不一致 0；200 组均取得零 gap 证书 |
| 两池 20 等分索引网格 | 同一批 200 组，分配为 `floor(A*k/20)`、`A-x` | 与相同网格穷举不一致 0；200 组均取得零 gap 证书 |
| 预算截断的界 | 两种拆单域各 30 次节点预算检查 | 已知最优全部位于 `[incumbent, upper_bound]` |
| 完整重跑 | 同一脚本与种子再次运行 | 结果 JSON 逐字节相同 |

工作量补充：

- 图搜索：历史签名 5,664 次报价；穷举 21,791 次。
- 全整数拆单：候选报价 1,426 次；逐整数穷举报价 228,984 次；另有 2,042 次有理数切线计算。
- 20 等分索引网格：候选报价 972 次；相同网格穷举报价 8,000 次；另有 1,119 次有理数切线计算。

**不能把这些数字报告为对仓库 `direct_split` 或 `metis_inspired` 的加速。**CPMM 报价本身很便宜，`Fraction` 上界可能比省下的报价更贵；这里没有记录可用于采用决策的 wall-clock 数据。网格余数分配也只是本验证器的明确约定，未核对仓库的 dust 规则。

生成器采用独立随机储备，可能形成强烈跨池错价；“7/200”不是现实发生概率。通过这些测试也不证明 CL、LB、共享池、多块计划或 token DAG admission 的正确性。

---

## 3. 新算法方向与完整验证要求

### N1. History/admission-aware labels：按未来可行性，而不是只按金额合并

**动机。**单 label 或纯 top-k 可能用一个高金额但不能接最佳后缀的前缀，覆盖掉低金额可续接前缀。见 §2.1–2.2。

**具体机制。**把 label 写成：

\[
L=(h,v,a,S,R,\sigma),
\]

其中 h 是跳数，v 是当前 token，a 是金额，S 是已访问 token，R 是影响候选准入的图可达关系，σ 是未来仍可能使用到的池状态。

一个保守的金额支配判定至少要求：

1. h、v 相同；
2. 被保留 label 的可行续接集合包含被删 label 的可行续接集合；
3. 相同续接在两者下使用可比较的池状态，报价与成功条件满足所需单调性；
4. 被保留金额不小于被删金额。

在本轮**固定当前块初始池状态、简单路径、无额外 DAG admission、每条路径不重复池、完整输入可报价**的 CPMM 模型里，相同 `(h,v,S)` 的未来池不受前缀影响，较大金额足以保证单块最优 gross 不丢失。这就是脚本中 `history` 模式的证明边界。

移到仓库时不能照抄：

- 有 prefix-dependent admission 时，S 可能不够，需要当前计划加前缀形成的 reachability/forbidden-relation 签名。
- 允许重复池时，需要状态信息或禁止不安全合并；hash 不能替代状态相等性的可靠判定。
- 更大输入可能触发完整成交失败的协议接口，不能只用“输出单调”论证可行性支配。
- 相同最终 gross 的不同前缀会留下不同池状态；**单块最优金额一致，不保证跨块计划相同或最终多块输出一致**。
- 若要求与旧策略完全相同的平局计划，必须单独证明 tie 语义；金额支配仅证明 gross 最优值。

**预期收益/代价。**预期减少等价前缀冗余、避免部分漏解；代价是更多 label、状态签名与支配检查。没有真实语料收益估计。

**适用/退化。**小 token 宇宙、多平行池、简单路径域最值得试。不同 admission/pool-state 签名大量增长时可能接近穷举。达到硬预算必须显式截断；不能用“只保留 k 个”继续声称 exact。

**验证。**
- 已可运行：`python verify_research.py --output results.json`，看 `history_fixture`、`path_random`。
- 仓库阶段：在独立 worktree 加新命名实验实现；复跑 handoff 导航的 X4/X4b；对 4 跳 tuning 同一块状态逐块比较穷举。
- 入口模板：`uv run python main.py run --bundle "$C/bundle_tuning" --profile "$EXP_PROFILE" --strategies profile`。`$EXP_PROFILE` 必须先真实创建并通过配置检查，本轮未创建或运行。
- 指标：gross、合法完整计划、丢弃原因、token-revisit 与 prefix-admission 分项、label 总数/峰值、报价、状态比较成本、平局路径差异、最终多块回放。
- 不把 `label_relaxations` 和 `paths_scored` 直接作同单位比值。

**重复性核对。**与 handoff §6.1 的多 label 建议有关，但本提案把“保留几个”改为“什么时候可安全删除”，补充了状态、证明范围和跨块反例风险。不是声称发明了资源约束 label 算法 [B]。

### N2. Certified integer allocation：连续上界 + 整数可执行下界

**动机。**固定粗网格损失质量，局部细化没有全局证书，而直接把连续分配取整又可能不最优或不能执行。

**已定义的问题。**本轮最小实例只有两个不共享池的直连 CPMM，输入 A：

\[
\max_{x\in D}\;q_1(x)+q_2(A-x),
\]

D 明确选为全部整数 `0..A`，或 20 等分索引网格。没有跳数、共享池或未知目标变量。

**已实现的界。**令 `F(x)=f_1(x)+f_2(A-x)`，f 为移除最终 floor 后的连续 CPMM 输出。F 为凹函数，故任意 z 的切线满足：

\[
G(x)\le F(x)\le F(z)+F'(z)(x-z).
\]

对未搜索区间 `[l,u]`，取：

\[
U_z(l,u)=\left\lfloor
F(z)+F'(z)\big((u\ \mathrm{if}\ F'(z)\ge0\ \mathrm{else}\ l)-z\big)
\right\rfloor.
\]

对端点和中点的几个安全界取最小值，仍是安全上界；实际整数候选给下界 L。只有 `U<=L` 才按 gross 值剪枝。上界用精确有理数计算，避免浮点向下误差造成误剪。

**证书边界。**`U=L` 证明在 D 内 gross 最优，不保证选中与另一个求解器相同的平局分配。预算耗尽则保留 incumbent 和未探索的 U，返回 `timeout`，不能伪装成 `no_route`。

**扩展路线。**
- 先把相同思想作用于固定候选的 `direct_split`。
- 多路径时可把单路径单调凹包络组合成松弛；对于共享池，不能套用独立路径相加，需池级流模型与可执行计划恢复。
- LP/对偶松弛可以忽略部分冲突或拆单数限制以获得偏乐观界，但必须证明它包含原可行域。
- 对手中候选集合的证书不能包装为整个图的最优证书。
- 没有通过上界证明的 CL/LB 类型应返回未知/无穷界，而不是猜测曲线。LB 动态费用依赖逐 bin 和历史状态 [L]；这不是直接套用 CPMM 切线的理由。
- 多跳的整数误差要按最终单位传播，不能简单按跳数相加，见 §2.5。

**预期收益/代价。**有望在候选报价昂贵时减少必需报价，并提供明确未解 gap；代价是包络、分支管理与精确界计算。小额平台、很松的界或廉价 CPMM 报价可能使它退化得比已有 DP 更慢。

**验证。**
- 已可运行：同一脚本的 `split_random` 与 `split_grid20`。
- 仓库阶段必须先复制其真实 percent/dust/max_splits 语义，再与 `direct_split` 做**同网格**比较；随后才能单独扩大金额域。
- 运行入口仍为上述 `main.py run ... --strategies profile`；新实现/新 profile 未完成，不提供虚构注册名的现成命令。
- 指标：与同域穷举的输出差异、证书是否覆盖最优、回放、报价、bounds 次数、总耗时、峰值内存、timeout 比例及 gap。界成本必须记入总预算。

**重复性核对。**连续 CFMM 优化及对偶分解已有明确研究 [C1,C2]，handoff 也把它们登记为未来方向。本提案不是重新推荐来源不明的 Brent 法，而是定义了优化变量、约束、整数下界、安全上界与停止规则；已经验证的仅为两池 CPMM 小域。

### N3. Rollback-and-replay suffix repair：有限回退，完整重算

**动机。**对贪心已提交块做局部调整，可能修复早期选择造成的流动性与结构锁定。

**具体机制。**
1. 保留原完整可执行计划 P 作为 incumbent。
2. 只在预先定义的块边界保存完整池状态与 token 余额；选择一个短后缀回滚。
3. 在回滚状态下重新生成后缀候选，不复用依赖旧状态的输出。
4. 从原始冻结状态独立回放整份候选计划 P′，核对输入守恒、余额、池状态、准入和预算。
5. 仅当合法且 `Q(P′)>Q(P)` 时替换；相等输出按预登记规则处理。

**预期收益/代价。**在完成原 incumbent 且预算允许的条件下，严格接受规则保证输出不退步；不保证总延迟不增加、不保证新计划产生、不保证全局最优。成本是回滚状态、重复搜索和完整回放。

**适用/退化。**共享池/中间 token 的早期结构选择错误最相关。错误出现在回滚窗口之前、候选被 DAG 禁止、或预算已耗尽时不会奏效。不能把重跑多起点的额外预算免费隐去。

**验证。**先构造一个可复现的多块贪心陷阱 fixture（本轮尚未完成）；再只在 tuning 上选一个固定回退长度与预算。用现有 `incremental_graph` 完整计划作为同预算账本中的基线，记录 candidate acceptance、gross、失败回放、额外报价/时间。仓库命令需待该新 profile 和 fixture 存在后使用 `main.py run` 模板；现有基础检查见 §6。

**重复性核对。**类别上与 handoff §6.9 相同，并非完全独立的新想法；这里的新增是具体回滚对象、状态失效边界和只能接受完整回放改进的规则。本轮只有提案，没有声称它已经改善语料。

---

## 4. 现有策略的优化点

| 优化点 | 动机、预期收益与代价 | 范围、退化与如何验证 | 是否重复 |
|---|---|---|---|
| 精确层 L02–L05 的重新采用评估 | 利用已存在的精确控制，避免先改搜索就扩大验证面；代价是干净计时和完整语义对照。 | 重放冻结 session 的真实 `replay_command`，并执行全测试；统一机器、负载规则、热/冷缓存、memory 轮次。不能从减少逻辑步直接宣布延迟收益。本轮未取到 driver，不编造 L08 命令。 | 是已有实验的复测，不是新算法。 |
| `max_splits` 固定扫描 | 检查当前拆单上限是否合理；收益未知，代价可能是组合空间增长，不能先称“成本低”。 | 仅 tuning，建议 `{1,2,4,8}`；同候选/网格/预算，并记录预算是否截断。已附 `prepare_splits_scan.py` 生成独立 profile，命令见 §6。该参数未必作用于图分块策略，分算法解释。 | handoff §6.10 已提出，本轮给可复现的配置生成方式。 |
| 独立 cycle-safe SOR 变体 | 在候选组合阶段检查计划 token DAG，避免选择后才 invalid；增加准入检查成本。 | 不改 `uni_sor_port` parity。先测合成环/非环/平局；旧 report 失败例只能作已知缺陷回归，不用于调参。需确认适配层与上游选择契约。 | handoff 已建议，本轮不当作新发现。 |
| LB 覆盖和搜索质量分拆 | 扩协议可能改善覆盖，但不是同能力搜索改善；增加报价/适配/测试面。 | 在新命名变体中做；五源与 matched cohort 分开。不能将历史 p95 差距当作新增 LB 的收益上限。 | handoff 已登记；未实现。 |
| 短名单/采样加“遗漏是否可能影响最优”诊断 | 精确报价层无法找回已删候选；希望暴露候选遗漏风险而非只看平均损失。 | 无可靠上界时保持启发式身份；不要以两点排名作证明。报价预算要包含 probe、refine 和候选外证书成本。 | 区别于重调 L06/L07 参数，但与 N2 共用界的研究。 |

这些优化均不能直接改默认行为。本轮没有默认采用建议；保留/采用需要真实语料、正确性和测量证据。

---

## 5. 建议的首个仓库实验与优先级

### 5.1 先恢复证据入口，再做一个小而可归因的实验

**优先顺序：**

1. **证据入口**：获得可读 checkout、工作流和冻结 bundle；核验 commit、bundle hash、split、profile。否则不做源码层结论。
2. **N1 同深度状态完备性实验**：先用 fixture 和 tuning 逐块对照，不急于跑九策略全 report。
3. **N2 同网格 CPMM 验证**：以真实 `direct_split` 的舍入/余数语义替换独立模型语义，再量化界开销。
4. **精确控制复测与 max_splits 扫描**：低语义风险，但不同于保证低运行成本。
5. **N3 有限回退**：在状态、合法性与预算账本齐全之后做。
6. **LB 覆盖与成本标定**：单独立项，不混入纯 gross 搜索实验结论。

### 5.2 分开跳数和机制：2×2 对照

所有格子固定相同 corpus、chunk 数、目标、报价/候选/时间预算与确定性规则：

|  | 3 跳 | 4 跳 |
|---|---|---|
| 枚举式单块候选选择 | E3 | E4 |
| 现有 label 式单块候选选择 | L3 | L4 |

再加新签名状态的 S3/S4，而不是直接拿 S4 对 E3 宣称“新算法更好”。

handoff 所述既有 M4 与 A0 的比较同时改变深度和机制 [H，§5.2]。本轮没有重跑这些 arm；表中的 E/L/S 是实验设计标签，不是已存在的仓库注册名。

逐 case 的普通 bps 仍定义为：

\[
10^4(Q_{\rm candidate}/Q_{\rm baseline}-1).
\]

因果分解应使用比率恒等式或对数：

\[
Q_{L4}/Q_{E3}=(Q_{E4}/Q_{E3})(Q_{L4}/Q_{E4}).
\]

不要把不同分母的两个 bps 直接相加。分别报告“枚举下的深度效应”“同深度的机制效应”及二者交互。

### 5.3 冻结和评价纪律

- 探索：仅 `bundle_tuning`；相同能力对比另外使用 tuning matched cohort，但先核对其真实目录名。
- nominee 与门槛：看 report 前冻结实现、参数、case 清单、预算、tie 规则、fallback、指标和 SHA。
- report：只作一次冻结判定；预算截断/invalid/no_route 必须全数报告。不能只保留双边成功 case 后省略失败率。
- 旧 report 已经参加过其他 campaign；新的预登记不把它恢复为从未见过的数据。它仍可用于新的冻结比较，但对跨区块推广最好增加真正未参与选择的新冻结区块。
- 数值优先：计划独立回放、完整输入守恒、状态一致、有效性零新增回归；错误结果不能通过平均 gross 掩盖。
- 性能：同时报告报价、底层数学步、搜索/准入工作量、缓存占用和 wall-clock。节点/relaxation/path 的单位不同，分别列示。
- 统计：金额档、方向和同一 token 对可能相关；按合理 case family 分组显示收益，不只给一个把所有 case 当独立样本的 p 值。
- incumbent wrapper 的额外搜索、验证和预算必须全部计入。同一输出上的平局计划也可能改变后续块，需单独记录。
- 采用阈值属于 owner 决策，本轮不捏造“可接受损失 1 bps”之类业务要求。

---

## 6. 可复现命令

### 6.1 本轮已经实际运行

需要 Python 3.10 或更高版本，无第三方依赖：

```bash
python verify_research.py --output results.json
python verify_research.py --output results-repeat.json
cmp results.json results-repeat.json
python -m py_compile verify_research.py
```

默认种子：路径生成器 `20260929`；拆单生成器 `20260930`。这些是固定整数种子，不代表两天分别进行了实验。

算法差异、所有 400 组拆单记录、预算界检查的汇总都在 JSON 内。原始标准输出为 `run.log`。完整重跑已通过字节一致性检查。

### 6.2 目标仓库基础检查：列出但本轮未运行

先取得并遵守 `docs/GIT_WORKFLOW.md`；在独立 worktree 冻结目标源码。以下接口来源为 handoff §7：

```bash
uv run python docs/examples/routing-algorithms/run_examples.py
uv run pytest tests/docs -q
uv run pytest -q
uv run ruff check .
uv run mypy

uv run python main.py quote \
  --bundle tests/fixtures/corpus/bundle \
  --profile config/daily_gross.yaml \
  --token-in USDC --token-out USDT0 --amount 10000 --details
```

不要把本验证器的通过数量并入上述仓库测试数量。

### 6.3 九策略探索基线：只在 tuning

以下命令是 handoff 接口的明确化，不是本轮已执行的作业。`C` 必须指向真实、核验过的冻结语料，不能指向 19 池 fixture 冒充完整语料。

```bash
C=/absolute/path/to/data/corpus/mantle-5src-101082044

uv run python main.py validate --bundle "$C/bundle"

uv run python main.py run \
  --bundle "$C/bundle_tuning" \
  --profile config/daily_gross.yaml \
  --strategies all

uv run python main.py run \
  --bundle "$C/bundle_tuning" \
  --profile config/full_gross.yaml \
  --strategies all
```

`all` 的原生深度不同，因此这是运营视角的“各自 profile 表现”，不是纯机制消融。输出应同时登记 `search.max_hops` 与 `graph.label_hops`。

### 6.4 max_splits 扫描

附带 [`prepare_splits_scan.py`](prepare_splits_scan.py)。它依赖 PyYAML，只复制用户指定的 baseline，并修改已声明存在的 `search.max_splits`；不会覆盖已存在的输出文件，也不会修改 baseline。

在符合工作流的独立 worktree 中运行：

```bash
uv run python /absolute/path/to/prepare_splits_scan.py \
  --baseline config/full_gross.yaml \
  --out-dir config/research-splits

for s in 1 2 4 8; do
  uv run python main.py run \
    --bundle "$C/bundle_tuning" \
    --profile "config/research-splits/s${s}.yaml" \
    --strategies base
done
```

这是计划命令。目标仓库的 YAML schema 和 CLI 尚未独立验证；因此脚本采用字段存在性检查，生成后还需仓库正常解析/测试。`base` 会运行六个基础策略，不假装 CLI 支持任意算法名列表。

### 6.5 新算法与 held-out

新实现尚未注册；必须先完成 worktree 内实现、真实 profile 和测试，再使用：

```bash
uv run python main.py run \
  --bundle "$C/bundle_tuning" \
  --profile "$EXP_PROFILE" \
  --strategies profile
```

`EXP_PROFILE` 是待创建、待验证的文件，不能直接复制一个不存在的新策略名运行。最终冻结后，把 bundle 切到真实 `bundle_report` 才能做一次 held-out 判定。

报告与顺序比较接口：

```bash
uv run python main.py report data/results/ACTUAL_RUN_ID \
  --output data/reports/ACTUAL_REPORT_NAME

uv run python main.py order-check \
  data/results/ACTUAL_RUN_A data/results/ACTUAL_RUN_B
```

用真实 run ID 替换占位值；重跑优先使用 manifest 的 `replay_command`。不改写 WHI-1449 或 L08 的冻结结果。

---

## 7. 文档问题与建议修订

### D1. “LB 可测的上限”措辞不成立

handoff §6.6 把某个历史 p95 覆盖相关差距称为“可测的上限”。

p95 不是最大值，且不同策略之间的差距不是为固定策略加 LB 后的因果收益。因此建议改为：

> “历史语料上的覆盖相关差距，可作为动机，但不是新增 LB 的可达收益或收益上界。”

这是统计概念上的修订，不依赖重新跑实验。

### D2. “所有 L08 决策比较均 inconclusive”应限定比较集合

handoff §5.2 开头的全称表述，与后表指出 H1→H2 存在干净的 `opt_in_only` 比较，在文字上有张力。

可能的正确解释是“所有涉及默认采用的预登记比较均 inconclusive”，而非所有配对。建议明确集合。**未读到 L08 原报告，不能据此指控底层冻结证据自相矛盾。**

### D3. X4/X4b 的丢弃类别需要精确定义

“不能重复 token”可能指实际可行域禁环，也可能指高金额前缀占用 token 后，合法简单路径的较小金额前缀被删。多 label 只能在不改变域的情况下修复后一类。

建议每个 fixture 列出：枚举最优路径、它是否重复 token、在哪个状态丢失、是否被 evaluator 接受。不要仅凭 `token_revisit` 计数命名猜测。

### D4. 每条“最优”结论应登记完整域

至少列出：候选集、协议集、跳数、拆单数、金额网格和 dust、共享池、token/pool 重复规则、DAG admission、完整成交要求、目标和预算。

特别是 `all` 下原生深度不同，以及单块最优与整笔计划最优不同，不能只放一个笼统“最优性”标签。

### D5. 新增合成验证与真实结果的身份

建议把 future campaign 的 README 固定为：`source_sha`、`bundle_hash`、`split`、`profile_hash`、`candidate_domain`、`replay_semantics`、`budget`、`timing_eligibility`、`preregistration_id`、`reuse_of_prior_holdout`。

本轮没有发现可确认的源码缺陷；得到的是可复核的数学反例、设计边界与文档措辞问题。

---

## 8. 交付物与下一次研究的判定目标

本包包含：

- `research-report.md`：本报告。
- `verify_research.py`：独立、确定性数学验证器。
- `results.json`：本轮逐例记录与汇总。
- `run.log`：实际运行输出。
- `prepare_splits_scan.py`：未在目标仓库运行的配置生成辅助脚本。
- `provenance.json`：成功读取的资料、访问限制、源码身份与本轮产物校验信息。
- `README.md`：快速复现入口。

**下一次最有价值的判定不是“k=几比较好”，而是：**

> 在相同初始块状态、相同深度、相同准入规则下，历史/准入签名是否能解释并消除已知前缀丢失，同时用可接受的额外状态成本保留确定性和完整计划回放？

本轮证据支持进入这个实验，不支持默认替换任何现有策略。

---

## 参考资料

[H] 用户提供的 handoff，成功读取，章节引用为本轮仓库上下文依据：  
https://github.com/Whisker17/router-algorithms-optimizer/blob/dev/docs/handoff/algorithm-exploration-handoff.md

[U] Uniswap 官方 `UniswapV2Library.sol`，`getAmountOut` 整数公式：  
https://github.com/Uniswap/v2-periphery/blob/master/contracts/libraries/UniswapV2Library.sol

[B] Boost Graph 官方 Resource-Constrained Shortest Paths 文档，资源容器与支配关系契约：  
https://www.boost.org/doc/libs/latest/libs/graph/doc/html/graph/algorithms/shortest_paths/r_c_shortest_paths.html

[C1] Angeris, Evans, Chitra, Boyd, *Optimal Routing for Constant Function Market Makers*，作者页面：  
https://stanford.edu/~boyd/papers/cfmm_routing.html

[C2] Diamandis, Resnick, Chitra, Angeris, *An Efficient Algorithm for Optimal Routing Through Constant Function Market Makers*，2023，论文 HTML：  
https://arxiv.org/html/2302.04938

[L] LFJ 官方 Liquidity Book 费用文档，逐 bin 动态费用与历史状态：  
https://developers.lfj.gg/concepts/fees

说明：这里用 [L] 说明 LB 类模型不能无条件套用固定费 CPMM 证明；它不替代对仓库 Merchant Moe LB 版本、参数及实现的逐项核验。
