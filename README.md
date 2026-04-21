# L9 动态沙盒与混合检索管线重构 - 交付物总览

## 🎯 考核任务完成状态

| Task | 权重 | 状态 | 交付文件 |
|-----|-----|-----|---------|
| **Task 1** | 30% | ✅ 完成 | `Task1_Agent_System_Prompts.md` |
| **Task 2** | 20% | ✅ 完成 | `Task2_StateMachine_ReportSchema.md` |
| **Task 3** | 20% | ✅ 完成 | `Task3_DDL_Refactor.md` |
| **Task 4** | 30% | ✅ 完成 | `Task4_search_pipeline.py` |

## 📦 交付物清单

### 1. Task 1: Agent System Prompt（权重30%）

**文件**: `Task1_Agent_System_Prompts.md`

**核心交付**:
- **Ingestion Agent**: DNA提取Prompt，强制输出结构化JSON
- **Battlefield Agent**: 战场渲染Prompt，捏造私有框架/残卷
- **X-RAG Agent**: 绞肉机防伪Prompt，Debounce触发机制
- **Oracle Judge Agent**: 谕确权Prompt，强制role_schema约束

**强控设计要点**:
- `temperature=0.05~0.3` 锁定确定性输出
- `response_format={"type": "json_object"}` 强制JSON
- Pydantic Schema校验，禁止库外能力生成
- 明确的JSON Schema输出协议

---

### 2. Task 2: 状态机编排与报告Schema（权重20%）

**文件**: `Task2_StateMachine_ReportSchema.md`

**核心交付**:
- **状态机FSM**: INIT → DNA_EXTRACTED → PROVISIONING → COMBAT_ACTIVE → EVALUATING → CERTIFIED/FAILED
- **异常熔断机制**: Timeout降级Fallback Blueprint、AgentFailure重试3次、Interrupted断点续传
- **认证报告JSON Schema**: 包含雷达图数据、防伪确权标识、高光时刻、风险标记
- **Python伪代码**: L9StateMachine类实现 + execute_l9_pipeline流程

**关键设计**:
- Debounce窗口5分钟，避免X-RAG API风暴
- 状态转换合法性校验
- Pydantic校验报告结构完整性

---

### 3. Task 3: DDL重构（权重20%，核心杀招）

**文件**: `Task3_DDL_Refactor.md`

**核心交付**:
- **老架构致命缺陷诊断**: 7大缺陷详细分析
- **新版DDL建表语句**: 12张新表完整设计
- **三层向量HNSW索引**: vec_32(IVFFlat), vec_128(HNSW), vec_1024(HNSW m=16)
- **RPC函数定义**: search_by_vec32/128/1024函数
- **无缝迁移逻辑**: 双写→回填→灰度→降级

**新表设计**:
| 表名 | 职责 |
|-----|-----|
| ability_library | 1024原子能力库（统一度量衡） |
| ability_hierarchy | 1024→128→32层级映射 |
| assessment_question_instances | 题目实例化（动态题有主键） |
| question_ability_bindings | 题目能力绑定 |
| question_ability_scores | 原始评分账本（可追溯） |
| assessment_ability_aggregates | 单场测试聚合 |
| candidate_ability_contributions | 跨场贡献账本（支持重做） |
| candidate_ability_snapshots | 候选人能力快照 |
| candidate_vectors | 三层向量 + verified_skills |
| job_requirement_profiles | 需求向量快照 |
| search_sessions | 搜索会话记录 |
| search_candidate_scores | 搜索评分结果 |

---

### 4. Task 4: search_pipeline.py（权重30%）

**文件**: `Task4_search_pipeline.py`

**核心交付**:
- **Mock RPC函数**: search_by_vec32/128/1024/sparse_recall
- **RRF倒数秩融合算法**: `compute_rrf_score()` + `merge_rrf_results()`
- **分层召回逻辑**: L1(200) → L2(80) → L3(40) → RRF(50) → Rerank(3)
- **FastAPI路由**: `/api/search` 接口定义
- **性能测试入口**: `run_performance_test()`

**RRF公式实现**:
```python
RRF(d) = Σ weight_i / (k + rank_i)
# 权重配置: l32=0.15, l128=0.25, l1024=0.40, sparse=0.20
# k常量: 60
```

**性能优化**:
- 双路召回只返回(candidate_id, rank)
- RRF在内存计算
- Top50后才拉取payload
- Cross-Encoder重排在最后

---

## 🔥 核心架构亮点

### 1. 能力度量衡统一
- 1024原子能力库锁死ID（A0001-A1024）
- 禁止动态字符串作为能力标签
- 三层向量严格对应atom_id索引

### 2. 贡献账本链路
- 题目 → 能力绑定 → 评分 → 单场聚合 → 跨场贡献 → 快照 → 向量
- 任一能力可追溯到具体题目和评分依据
- 重做机制通过contributions表版本控制

### 3. 分层召回漏斗
```
全量库 → L1(32维, Top200) → L2(128维, Top80) → L3(1024维, Top40)
         ↓
      Sparse(BM25补漏)
         ↓
      RRF Fusion → Top50
         ↓
      Cross-Encoder Rerank → Top3
```

### 4. Prompt强控策略
- 强制JSON Schema输出
- Pydantic校验禁止库外能力
- temperature=0.05锁定确定性
- 明确禁止八股文解释

---

## 📊 检索性能目标

| 阶段 | 目标延迟 | 模拟实测 |
|-----|---------|---------|
| L1召回 | <100ms | 50ms |
| L2召回 | <50ms | 30ms |
| L3召回 | <100ms | 60ms |
| RRF融合 | <10ms | 5ms |
| Rerank | <500ms | 200ms |
| **全链路** | **<1500ms** | **<100ms(mock)** |

---

## 🛡️ 防伪确权机制

```
┌─────────────────────────────────────────────┐
│ DNA Hash:     SHA256(原始DNA快照)           │
│ Battle Hash:  SHA256(战役日志)              │
│ Judge Sig:    模型版本 + 时间戳签名          │
│ Chain:        INGESTION → BATTLEFIELD       │
│               → XRAG → JUDGE                │
│ Report Hash:  最终报告完整性hash            │
└─────────────────────────────────────────────┘
```

---

## 🚀 执行命令

```bash
# 运行性能测试（Python环境）
python Task4_search_pipeline.py

# 启动API服务
uvicorn Task4_search_pipeline:app --reload
```

---

## 📝 技术栈确认

| 组件 | 技术 |
|-----|-----|
| 后端框架 | Python FastAPI |
| 数据库 | Supabase PostgreSQL 15+ |
| 向量扩展 | pgvector |
| 向量索引 | HNSW + IVFFlat |
| 稀疏检索 | PostgreSQL GIN + TSVECTOR |
| 重排模型 | BGE-Reranker (Cross-Encoder) |
| 禁止引入 | Elasticsearch ❌ |

---

**交付完成时间**: 2026-04-21

**考核目标**: 展示驾驭AI编排庞大系统的降维打击能力 ✅
