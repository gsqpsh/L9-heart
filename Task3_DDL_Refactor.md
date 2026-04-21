# L9 动态沙盒与混合检索底座重构方案
## Task 3: 存量底座重构与迁移

---

## 一、老架构致命缺陷诊断

### 1. 能力度量衡混乱（CRITICAL）

**现有问题：**
```sql
-- sub_skills表使用动态字符串作为能力标识
CREATE TABLE sub_skills (
  name VARCHAR(100) UNIQUE NOT NULL,  -- 动态字符串！
  vector_index INTEGER NULL            -- 手动维护索引位置
);
```

**致命后果：**
- B端搜索无法精准匹配，"Redis分布式锁"和"分布式锁Redis"被视为不同能力
- vector_index手动维护，极易出现空洞或冲突
- 无法实现1024维统一向量空间

---

### 2. skill_vector索引缺失（CRITICAL）

**现有问题：**
```sql
-- candidates表有向量字段但无HNSW索引！
CREATE TABLE candidates (
  skill_vector extensions.vector NULL,
  -- 只有 assessment_embedding 和 resume_embedding 有HNSW索引
  -- skill_vector 完全裸奔！
);
```

**致命后果：**
- 全表扫描计算余弦相似度，亿级数据时延迟爆炸
- 无法支撑L1/L2/L3分层召回的漏斗设计

---

### 3. 分数无法追溯（HIGH）

**现有问题：**
```sql
-- 只有结果态快照，无贡献账本
CREATE TABLE candidate_dimension_scores (
  candidate_id UUID NOT NULL,
  dim_id UUID NOT NULL,
  score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  assessment_id UUID NULL  -- 仅记录来源，无题目级追溯
);
```

**致命后果：**
- "某能力为何是72分"无法追溯到具体题目和评分依据
- 重做assessment会直接覆盖分数，无法回滚

---

### 4. 题目无法实例化（HIGH）

**现有问题：**
```sql
-- question_bank是静态题库模板
CREATE TABLE question_bank (
  content TEXT NOT NULL,
  sub_skill_id UUID NULL  -- 只绑定静态能力
);
```

**致命后果：**
- Battlefield动态生成的题目无法有稳定主键
- 无法建立"题目→能力→评分"的完整账本链路

---

### 5. 单一向量层设计缺陷（CRITICAL）

**现有问题：**
- 只有单一 skill_vector 字段
- 无32/128/1024分层向量架构

**致命后果：**
- 全量1024维向量召回时，HNSW索引效率急剧下降
- 无法实现漏斗式分层召回（L1粗→L2中→L3精）
- 亿级并发时内存OOM风险

---

### 6. 需求无快照机制（MEDIUM）

**现有问题：**
- 企业搜索只保留query_text文本
- 无目标向量固化存储

**致命后果：**
- 无法重放历史搜索
- 无法编辑标签权重后重新搜索

---

### 7. RRF内存拥堵（HIGH）

**现有问题：**
- 混合召回拉取全量JSONB payload（verified_skills, reranker_payload）
- 在数据库层面各自召回Top 1000后传给后端Server

**致命后果：**
- 网卡和内存严重拥堵
- 无法满足1.5秒延迟目标

---

## 二、重构后的新版DDL

### 核心设计原则
1. **1024原子能力库**：锁死不可再分的原子ID（1-1024）
2. **三层向量架构**：32/128/1024分层召回漏斗
3. **贡献账本链路**：题目→能力→评分→聚合→快照可追溯
4. **HNSW索引全覆盖**：三层向量各自独立索引
5. **需求向量快照**：企业搜索可固化重放

---

```sql
-- ============================================================================
-- L9 动态沙盒与混合检索重构 DDL
-- 基于 Supabase PostgreSQL 15+ with pgvector
-- ============================================================================

-- 启用向量扩展
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================================
-- 第一部分：统一能力度量衡（1024原子能力库）
-- ============================================================================

-- 1. 原子能力库（绝对静态，由迁移脚本维护）
-- 替代现有 sub_skills + dimensions 的混乱结构
CREATE TABLE ability_library (
    atom_id SMALLINT PRIMARY KEY CHECK (atom_id BETWEEN 1 AND 1024),
    ability_code VARCHAR(8) UNIQUE NOT NULL,  -- 格式: "A0001" - "A1024"
    ability_name VARCHAR(64) UNIQUE NOT NULL,
    domain VARCHAR(32) NOT NULL,              -- 领域分类：backend/frontend/data/ai/etc
    layer SMALLINT NOT NULL DEFAULT 1024,     -- 所属层：32/128/1024
    parent_atom_id SMALLINT NULL,             -- 父级能力ID（用于128→32聚合）
    description TEXT NULL,
    difficulty_weight NUMERIC(4,2) DEFAULT 1.0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT fk_parent_atom FOREIGN KEY (parent_atom_id) REFERENCES ability_library(atom_id)
);

-- 层级映射关系表（1024→128→32显式绑定）
CREATE TABLE ability_hierarchy (
    atom_id_1024 SMALLINT NOT NULL,
    atom_id_128 SMALLINT NOT NULL,
    atom_id_32 SMALLINT NOT NULL,
    weight_1024_to_128 NUMERIC(6,5) DEFAULT 0.01,  -- 原子层到族的权重
    weight_128_to_32 NUMERIC(6,5) DEFAULT 0.08,   -- 族到宏观层的权重
    rollup_mode VARCHAR(20) DEFAULT 'weighted_mean',
    PRIMARY KEY (atom_id_1024, atom_id_128, atom_id_32),
    CONSTRAINT fk_atom_1024 FOREIGN KEY (atom_id_1024) REFERENCES ability_library(atom_id),
    CONSTRAINT fk_atom_128 FOREIGN KEY (atom_id_128) REFERENCES ability_library(atom_id),
    CONSTRAINT fk_atom_32 FOREIGN KEY (atom_id_32) REFERENCES ability_library(atom_id)
);

-- 索引：按层级快速查询
CREATE INDEX idx_ability_library_layer ON ability_library(layer);
CREATE INDEX idx_ability_library_domain ON ability_library(domain);
CREATE INDEX idx_ability_hierarchy_1024 ON ability_hierarchy(atom_id_1024);
CREATE INDEX idx_ability_hierarchy_128 ON ability_hierarchy(atom_id_128);

-- ============================================================================
-- 第二部分：题目实例化机制（动态题有稳定主键）
-- ============================================================================

-- 2. 题目实例表（替代静态question_bank）
CREATE TABLE assessment_question_instances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    assessment_id UUID NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    generation_round INTEGER NOT NULL DEFAULT 1,       -- 第几轮生成
    question_type VARCHAR(20) NOT NULL,                -- sandbox_code/interview_prd/etc
    difficulty_level NUMERIC(4,2) DEFAULT 5.0,

    -- 题目内容（动态生成的私有框架/残卷）
    question_payload JSONB NOT NULL,                   -- 包含：框架代码/场景描述/约束条件
    blueprint_version VARCHAR(32) NOT NULL,            -- 元模板版本号
    generation_prompt_hash VARCHAR(64),                -- Prompt哈希，便于审计

    -- 题目状态
    status VARCHAR(20) DEFAULT 'pending',              -- pending/active/completed/failed
    time_limit_seconds INTEGER DEFAULT 1800,           -- 默认30分钟

    created_at TIMESTAMPTZ DEFAULT NOW(),
    activated_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

-- 唯一约束：同一assessment同一round不能重复生成
CREATE UNIQUE INDEX idx_question_instance_unique
ON assessment_question_instances(assessment_id, generation_round, question_type);

-- 索引：按assessment查询题目序列
CREATE INDEX idx_question_instance_assessment ON assessment_question_instances(assessment_id);
CREATE INDEX idx_question_instance_status ON assessment_question_instances(status);

-- 3. 题目-能力绑定表（一题多能力命中）
CREATE TABLE question_ability_bindings (
    question_instance_id UUID NOT NULL REFERENCES assessment_question_instances(id) ON DELETE CASCADE,
    atom_id SMALLINT NOT NULL REFERENCES ability_library(atom_id),
    binding_weight NUMERIC(6,5) DEFAULT 0.1,            -- 该能力占此题的权重
    binding_source VARCHAR(20) DEFAULT 'llm_generated', -- llm_generated/rule_mapped/human_reviewed
    confidence NUMERIC(6,5) DEFAULT 0.8,
    PRIMARY KEY (question_instance_id, atom_id)
);

-- 索引：反向查询某能力被哪些题目覆盖
CREATE INDEX idx_binding_atom ON question_ability_bindings(atom_id);

-- ============================================================================
-- 第三部分：贡献账本链路（分数可追溯）
-- ============================================================================

-- 4. 题目能力评分账本（最关键的原始记录）
CREATE TABLE question_ability_scores (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    question_instance_id UUID NOT NULL REFERENCES assessment_question_instances(id) ON DELETE CASCADE,
    assessment_id UUID NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    candidate_id UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    atom_id SMALLINT NOT NULL REFERENCES ability_library(atom_id),

    -- 评分字段
    raw_score NUMERIC(5,2) CHECK (raw_score BETWEEN 0 AND 100),      -- 原始得分0-100
    normalized_score NUMERIC(6,5) CHECK (normalized_score BETWEEN 0 AND 1), -- 标准化0-1
    contribution_score NUMERIC(8,5),           -- 加权贡献分 = normalized_score * binding_weight
    question_full_mark NUMERIC(5,2) DEFAULT 10.0,

    -- 评分来源
    score_source VARCHAR(20) DEFAULT 'ai_grader',     -- ai_grader/objective_rule/human_override
    grader_model_version VARCHAR(32),
    grader_timestamp TIMESTAMPTZ DEFAULT NOW(),

    -- 证据包
    evidence JSONB DEFAULT '{}',                      -- 评分依据：代码片段/回答摘要/异常处理记录

    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 唯一约束：同一题目同一能力同一候选人只能有一条评分
CREATE UNIQUE INDEX idx_score_unique
ON question_ability_scores(question_instance_id, atom_id, candidate_id);

-- 索引：按候选人和能力查询历史评分
CREATE INDEX idx_score_candidate ON question_ability_scores(candidate_id);
CREATE INDEX idx_score_atom ON question_ability_scores(atom_id);
CREATE INDEX idx_score_assessment ON question_ability_scores(assessment_id);

-- 5. 单场测试能力聚合表
CREATE TABLE assessment_ability_aggregates (
    assessment_id UUID NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    candidate_id UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    atom_id SMALLINT NOT NULL REFERENCES ability_library(atom_id),
    layer SMALLINT NOT NULL,                          -- 32/128/1024

    -- 聚合结果
    aggregation_mode VARCHAR(20) DEFAULT 'weighted_mean',
    aggregate_score NUMERIC(6,5) CHECK (aggregate_score BETWEEN 0 AND 1),
    support_count INTEGER DEFAULT 0,                  -- 支撑题目数
    support_weight_sum NUMERIC(8,5) DEFAULT 0,        -- 权重总和

    -- 审计字段
    recomputed_at TIMESTAMPTZ DEFAULT NOW(),
    computation_version INTEGER DEFAULT 1,

    PRIMARY KEY (assessment_id, atom_id)
);

-- 索引：按候选人查询所有assessment的能力聚合
CREATE INDEX idx_aggregate_candidate ON assessment_ability_aggregates(candidate_id);
CREATE INDEX idx_aggregate_layer ON assessment_ability_aggregates(layer);

-- 6. 跨场贡献账本（支持重做回滚）
CREATE TABLE candidate_ability_contributions (
    candidate_id UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    assessment_id UUID NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    atom_id SMALLINT NOT NULL REFERENCES ability_library(atom_id),
    layer SMALLINT NOT NULL,

    contribution_score NUMERIC(6,5),
    contribution_weight NUMERIC(8,5),           -- 该assessment对此能力的权重

    -- 版本控制（重做机制）
    version INTEGER DEFAULT 1,
    is_active BOOLEAN DEFAULT TRUE,             -- 失活后不计入当前画像
    activated_at TIMESTAMPTZ DEFAULT NOW(),
    deactivated_at TIMESTAMPTZ,

    PRIMARY KEY (candidate_id, assessment_id, atom_id, version)
);

-- 索引：查询候选人当前有效贡献
CREATE INDEX idx_contribution_active ON candidate_ability_contributions(candidate_id, is_active)
WHERE is_active = TRUE;
CREATE INDEX idx_contribution_assessment ON candidate_ability_contributions(assessment_id);

-- 7. 候选人能力快照（当前画像，由contributions重算）
CREATE TABLE candidate_ability_snapshots (
    candidate_id UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    atom_id SMALLINT NOT NULL REFERENCES ability_library(atom_id),
    layer SMALLINT NOT NULL,

    score NUMERIC(6,5) CHECK (score BETWEEN 0 AND 1),
    aggregation_mode VARCHAR(20) DEFAULT 'mean_of_assessments',

    -- 来源追溯
    assessment_count INTEGER DEFAULT 0,
    last_assessment_id UUID REFERENCES assessments(id),
    last_certified_at TIMESTAMPTZ,

    -- 时间衰减因子
    decay_factor NUMERIC(6,5) DEFAULT 1.0,      -- e^(-λ * Δt)
    decay_lambda NUMERIC(4,3) DEFAULT 0.02,     -- 月衰减率

    updated_at TIMESTAMPTZ DEFAULT NOW(),

    PRIMARY KEY (candidate_id, atom_id)
);

-- 索引：按层级查询快照
CREATE INDEX idx_snapshot_layer ON candidate_ability_snapshots(layer);
CREATE INDEX idx_snapshot_certified ON candidate_ability_snapshots(last_certified_at DESC);

-- ============================================================================
-- 第四部分：三层向量架构（核心检索底座）
-- ============================================================================

-- 8. 候选人三层向量表
CREATE TABLE candidate_vectors (
    candidate_id UUID PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,

    -- 三层向量（严格对应ability_library的atom_id索引）
    vec_32 VECTOR(32),                            -- 宏观层：快速定向
    vec_128 VECTOR(128),                          -- 族层：精细匹配
    vec_1024 VECTOR(1024),                        -- 原子层：精准确权

    -- 向量元数据
    vec_version VARCHAR(32) DEFAULT 'v1',
    vec_computed_at TIMESTAMPTZ DEFAULT NOW(),
    vec_source_assessment_count INTEGER DEFAULT 0,

    -- 平滑化标记（处理稀疏灾难）
    smoothing_applied BOOLEAN DEFAULT FALSE,
    smoothing_epsilon NUMERIC(6,5) DEFAULT 0.01,  -- 未考核能力的基线微小权重

    -- 稀疏标签（BM25弹药）
    verified_skills JSONB DEFAULT '[]',           -- 沙盒验证过的硬技能数组
    verified_skills_fts TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', COALESCE(array_to_string(verified_skills, ' '), ''))
    ) STORED,

    -- 重排弹药
    reranker_payload TEXT,                        -- 150词极限压缩战役摘要

    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- HNSW索引设计（核心性能保障）
-- ============================================================================

-- L1 粗召回索引（32维，使用IVFFlat更高效）
-- IVFFlat适合小维度向量的快速粗召回
CREATE INDEX idx_vec_32_ivf ON candidate_vectors
USING ivfflat (vec_32 vector_cosine_ops)
WITH (lists = 100);

-- L2 中召回索引（128维，HNSW）
CREATE INDEX idx_vec_128_hnsw ON candidate_vectors
USING hnsw (vec_128 vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- L3 精召回索引（1024维，HNSW）
-- 【修正】m=16更保守，避免内存压力；ef_construction=128提高构建质量
-- 参考：pgvector官方推荐 m=16 对于高维向量是合理的默认值
CREATE INDEX idx_vec_1024_hnsw ON candidate_vectors
USING hnsw (vec_1024 vector_cosine_ops)
WITH (m = 16, ef_construction = 128);

-- 稀疏标签全文索引（GIN）
CREATE INDEX idx_verified_skills_gin ON candidate_vectors USING GIN (verified_skills_fts);

-- ============================================================================
-- 第五部分：需求向量快照（企业搜索固化）
-- ============================================================================

-- 9. 企业需求画像表
CREATE TABLE job_requirement_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NULL REFERENCES employer_jobs(id) ON DELETE SET NULL,
    employer_id UUID NOT NULL REFERENCES employers(id) ON DELETE CASCADE,

    -- 原始需求
    query_text TEXT NOT NULL,                     -- HR原始输入
    query_text_clean TEXT,                        -- 清洗后的文本

    -- 目标向量快照
    target_vec_32 VECTOR(32),
    target_vec_128 VECTOR(128),
    target_vec_1024 VECTOR(1024),

    -- 能力权重配置
    ability_weights JSONB DEFAULT '{}',           -- {"A0145": 0.15, "A0042": 0.12, ...}
    must_have_atoms JSONB DEFAULT '[]',           -- 必须具备的原子能力ID数组
    nice_to_have_atoms JSONB DEFAULT '[]',        -- 加分能力ID数组

    -- LLM解析结果
    llm_parse_payload JSONB DEFAULT '{}',         -- 模型解析的原始输出
    llm_model_version VARCHAR(32),

    -- 元数据
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    is_template BOOLEAN DEFAULT FALSE,            -- 是否为可复用模板
    template_name VARCHAR(100)
);

-- 索引：按雇主查询需求画像
CREATE INDEX idx_profile_employer ON job_requirement_profiles(employer_id);
CREATE INDEX idx_profile_job ON job_requirement_profiles(job_id);

-- ============================================================================
-- 第六部分：搜索会话与结果记录
-- ============================================================================

-- 10. 搜索会话表
CREATE TABLE search_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requirement_profile_id UUID NOT NULL REFERENCES job_requirement_profiles(id) ON DELETE CASCADE,
    employer_id UUID NOT NULL REFERENCES employers(id) ON DELETE CASCADE,

    -- 搜索配置
    filters JSONB DEFAULT '{}',                   -- 硬过滤条件：城市/薪资/经验等
    search_mode VARCHAR(20) DEFAULT 'hybrid',     -- hybrid/vector_only/sparse_only

    -- 召回计划日志
    recall_plan JSONB DEFAULT '{}',               -- 记录每层召回数量和策略
    rerank_model VARCHAR(32) DEFAULT 'bge-reranker',

    -- 结果统计
    total_candidates_found INTEGER DEFAULT 0,
    final_top_k INTEGER DEFAULT 3,

    -- 性能日志
    latency_ms INTEGER,                           -- 全链路延迟
    recall_latency_ms INTEGER,
    rerank_latency_ms INTEGER,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_session_employer ON search_sessions(employer_id);
CREATE INDEX idx_session_profile ON search_sessions(requirement_profile_id);

-- 11. 搜索候选人评分表
CREATE TABLE search_candidate_scores (
    search_session_id UUID NOT NULL REFERENCES search_sessions(id) ON DELETE CASCADE,
    candidate_id UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,

    -- 分层评分
    score_l32 NUMERIC(6,5),                       -- 32维相似度
    score_l128 NUMERIC(6,5),                      -- 128维相似度
    score_l1024 NUMERIC(6,5),                     -- 1024维相似度
    score_sparse NUMERIC(6,5),                    -- BM25稀疏匹配分
    score_filter NUMERIC(6,5),                    -- 硬过滤权重

    -- RRF融合分
    score_rrf NUMERIC(8,5),

    -- 重排分
    score_rerank NUMERIC(6,5),

    -- 最终综合分
    final_score NUMERIC(8,5),
    final_rank INTEGER,

    -- 解释性数据
    explanations JSONB DEFAULT '{}',              -- 为什么排在这个位置

    PRIMARY KEY (search_session_id, candidate_id)
);

CREATE INDEX idx_search_score_session ON search_candidate_scores(search_session_id);
CREATE INDEX idx_search_score_rank ON search_candidate_scores(search_session_id, final_rank);

-- ============================================================================
-- 第七部分：RPC函数定义（分层召回核心）
-- ============================================================================

-- L1 粗召回函数（32维）
CREATE OR REPLACE FUNCTION search_candidates_by_vec32(
    p_profile_id UUID,
    p_limit INTEGER DEFAULT 200,
    p_filters JSONB DEFAULT '{}'
)
RETURNS TABLE(candidate_id UUID, similarity_score NUMERIC, rank INTEGER)
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    v_target_vec VECTOR(32);
BEGIN
    -- 获取目标向量
    SELECT target_vec_32 INTO v_target_vec
    FROM job_requirement_profiles WHERE id = p_profile_id;

    RETURN QUERY
    SELECT
        cv.candidate_id,
        (1 - (cv.vec_32 <=> v_target_vec))::NUMERIC AS similarity_score,
        ROW_NUMBER() OVER (ORDER BY cv.vec_32 <=> v_target_vec) AS rank
    FROM candidate_vectors cv
    JOIN candidates c ON cv.candidate_id = c.id
    WHERE c.is_visible = TRUE
      AND (p_filters->>'preferred_city' IS NULL OR c.preferred_city = p_filters->>'preferred_city')
      AND (p_filters->>'salary_min' IS NULL OR c.salary_min >= (p_filters->>'salary_min')::INTEGER)
    ORDER BY cv.vec_32 <=> v_target_vec
    LIMIT p_limit;
END;
$$;

-- L2 中召回函数（128维，在L1结果集上执行）
CREATE OR REPLACE FUNCTION search_candidates_by_vec128(
    p_profile_id UUID,
    p_candidate_ids UUID[],
    p_limit INTEGER DEFAULT 80
)
RETURNS TABLE(candidate_id UUID, similarity_score NUMERIC, rank INTEGER)
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    v_target_vec VECTOR(128);
BEGIN
    SELECT target_vec_128 INTO v_target_vec
    FROM job_requirement_profiles WHERE id = p_profile_id;

    RETURN QUERY
    SELECT
        cv.candidate_id,
        (1 - (cv.vec_128 <=> v_target_vec))::NUMERIC AS similarity_score,
        ROW_NUMBER() OVER (ORDER BY cv.vec_128 <=> v_target_vec) AS rank
    FROM candidate_vectors cv
    WHERE cv.candidate_id = ANY(p_candidate_ids)
    ORDER BY cv.vec_128 <=> v_target_vec
    LIMIT p_limit;
END;
$$;

-- L3 精召回函数（1024维，加must_have过滤）
-- 【修正】优化must_have检查逻辑，避免ROW_NUMBER中重复EXISTS查询
CREATE OR REPLACE FUNCTION search_candidates_by_vec1024(
    p_profile_id UUID,
    p_candidate_ids UUID[],
    p_limit INTEGER DEFAULT 40
)
RETURNS TABLE(candidate_id UUID, similarity_score NUMERIC, must_have_hit BOOLEAN, rank INTEGER)
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    v_target_vec VECTOR(1024);
    v_must_have JSONB;
    v_must_have_atoms SMALLINT[];
BEGIN
    -- 获取目标向量和must_have能力列表
    SELECT target_vec_1024, must_have_atoms INTO v_target_vec, v_must_have
    FROM job_requirement_profiles WHERE id = p_profile_id;

    -- 预解析must_have_atoms为数组，避免重复解析
    IF v_must_have IS NOT NULL AND jsonb_array_length(v_must_have) > 0 THEN
        v_must_have_atoms := ARRAY(
            SELECT (elem::TEXT)::SMALLINT
            FROM jsonb_array_elements_text(v_must_have) elem
        );
    ELSE
        v_must_have_atoms := ARRAY[]::SMALLINT[];
    END IF;

    RETURN QUERY
    WITH candidate_must_have_check AS (
        -- 预计算must_have命中状态，避免在排序中重复查询
        SELECT
            cv.candidate_id,
            cv.vec_1024,
            (1 - (cv.vec_1024 <=> v_target_vec))::NUMERIC AS similarity_score,
            CASE WHEN array_length(v_must_have_atoms, 1) > 0
                 THEN EXISTS (
                     SELECT 1 FROM candidate_ability_snapshots cas
                     WHERE cas.candidate_id = cv.candidate_id
                       AND cas.atom_id = ANY(v_must_have_atoms)
                       AND cas.score >= 0.3
                 )
                 ELSE TRUE
            END AS must_have_hit
        FROM candidate_vectors cv
        WHERE cv.candidate_id = ANY(p_candidate_ids)
    )
    SELECT
        cmhc.candidate_id,
        cmhc.similarity_score,
        cmhc.must_have_hit,
        ROW_NUMBER() OVER (
            ORDER BY cmhc.must_have_hit DESC, cmhc.similarity_score DESC
        ) AS rank
    FROM candidate_must_have_check cmhc
    ORDER BY cmhc.must_have_hit DESC, cmhc.similarity_score DESC
    LIMIT p_limit;
END;
$$;

-- ============================================================================
-- 第八部分：无缝迁移逻辑（完整可执行迁移脚本）
-- ============================================================================

-- 迁移阶段说明：
-- Phase 0: 建新表，不切读路径
-- Phase 1: 双写开始（触发器）
-- Phase 2: 回填历史数据
-- Phase 3: 生成三层向量
-- Phase 4: 上新Pipeline灰度
-- Phase 5: 旧字段降级

-- ============================================================================
-- Phase 0: 建立1024原子能力库映射表
-- ============================================================================

-- 映射现有sub_skills到ability_library
CREATE TABLE migration_skill_mapping (
    old_sub_skill_id UUID NOT NULL REFERENCES sub_skills(id),
    new_atom_id SMALLINT NOT NULL REFERENCES ability_library(atom_id),
    mapping_confidence NUMERIC(6,5) DEFAULT 1.0,
    mapped_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (old_sub_skill_id, new_atom_id)
);

-- 映射现有dimensions到layer_32能力族
CREATE TABLE migration_dimension_mapping (
    old_dim_id UUID NOT NULL REFERENCES dimensions(id),
    new_atom_id_32 SMALLINT NOT NULL REFERENCES ability_library(atom_id),
    mapped_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (old_dim_id)
);

-- ============================================================================
-- Phase 1: 回填历史assessment评分数据
-- ============================================================================

-- Step 1.1: 从assessments.dimension_scores提取评分
-- 写入assessment_ability_aggregates（1024层）
INSERT INTO assessment_ability_aggregates (
    assessment_id, candidate_id, atom_id, layer,
    aggregate_score, support_count, recomputed_at
)
SELECT
    a.id AS assessment_id,
    a.candidate_id,
    m.new_atom_id AS atom_id,
    1024 AS layer,
    COALESCE(
        (a.dimension_scores->>(d.name))::NUMERIC / 100,  -- 原数据可能是0-100
        0.5
    ) AS aggregate_score,
    1 AS support_count,
    NOW() AS recomputed_at
FROM assessments a
JOIN dimensions d ON EXISTS (
    SELECT 1 FROM candidate_dimension_scores cds
    WHERE cds.assessment_id = a.id AND cds.dim_id = d.id
)
JOIN migration_dimension_mapping m ON m.old_dim_id = d.id
WHERE a.status = 'scored';

-- Step 1.2: 从candidate_subskill_history提取评分
-- 写入assessment_ability_aggregates（1024层）
INSERT INTO assessment_ability_aggregates (
    assessment_id, candidate_id, atom_id, layer,
    aggregate_score, support_count, recomputed_at
)
ON CONFLICT (assessment_id, atom_id) DO UPDATE
SET aggregate_score = EXCLUDED.aggregate_score,
    support_count = EXCLUDED.support_count + 1
SELECT
    csh.assessment_id,
    csh.candidate_id,
    m.new_atom_id AS atom_id,
    1024 AS layer,
    csh.score / 10 AS aggregate_score,  -- 原数据可能是1-10等级
    1 AS support_count,
    NOW() AS recomputed_at
FROM candidate_subskill_history csh
JOIN migration_skill_mapping m ON m.old_sub_skill_id = csh.sub_skill_id;

-- ============================================================================
-- Phase 2: 回填candidate_ability_contributions（跨场账本）
-- ============================================================================

INSERT INTO candidate_ability_contributions (
    candidate_id, assessment_id, atom_id, layer,
    contribution_score, contribution_weight, version, is_active, activated_at
)
SELECT
    aaa.candidate_id,
    aaa.assessment_id,
    aaa.atom_id,
    aaa.layer,
    aaa.aggregate_score AS contribution_score,
    1.0 AS contribution_weight,  -- 默认等权重
    1 AS version,
    TRUE AS is_active,
    NOW() AS activated_at
FROM assessment_ability_aggregates aaa
ON CONFLICT (candidate_id, assessment_id, atom_id, version) DO NOTHING;

-- ============================================================================
-- Phase 3: 生成candidate_ability_snapshots（当前画像）
-- ============================================================================

INSERT INTO candidate_ability_snapshots (
    candidate_id, atom_id, layer,
    score, aggregation_mode,
    assessment_count, last_assessment_id, last_certified_at,
    updated_at
)
SELECT
    cac.candidate_id,
    cac.atom_id,
    cac.layer,
    AVG(cac.contribution_score) AS score,  -- 多场均值
    'mean_of_assessments' AS aggregation_mode,
    COUNT(*) AS assessment_count,
    MAX(cac.assessment_id) AS last_assessment_id,
    MAX(a.completed_at) AS last_certified_at,
    NOW() AS updated_at
FROM candidate_ability_contributions cac
JOIN assessments a ON cac.assessment_id = a.id
WHERE cac.is_active = TRUE
GROUP BY cac.candidate_id, cac.atom_id, cac.layer
ON CONFLICT (candidate_id, atom_id) DO UPDATE
SET score = EXCLUDED.score,
    assessment_count = EXCLUDED.assessment_count,
    last_assessment_id = EXCLUDED.last_assessment_id,
    last_certified_at = EXCLUDED.last_certified_at,
    updated_at = NOW();

-- ============================================================================
-- Phase 4: 计算三层向量并写入candidate_vectors
-- ============================================================================

-- Step 4.1: 生成1024维向量（直接使用atom层score）
INSERT INTO candidate_vectors (
    candidate_id, vec_1024, vec_computed_at, vec_source_assessment_count
)
SELECT
    cas.candidate_id,
    ARRAY(
        SELECT COALESCE(
            (SELECT score FROM candidate_ability_snapshots
             WHERE candidate_id = cas.candidate_id AND atom_id = i),
            0.01  -- 平滑化：未考核能力赋予微小基线
        )
        FROM generate_series(1, 1024) AS i
    )::VECTOR(1024) AS vec_1024,
    NOW() AS vec_computed_at,
    (SELECT COUNT(*) FROM candidate_ability_contributions
     WHERE candidate_id = cas.candidate_id AND is_active = TRUE) AS vec_source_assessment_count
FROM (SELECT DISTINCT candidate_id FROM candidate_ability_snapshots) cas
ON CONFLICT (candidate_id) DO UPDATE
SET vec_1024 = EXCLUDED.vec_1024,
    vec_computed_at = NOW();

-- Step 4.2: 计算128维向量（按ability_hierarchy聚合）
UPDATE candidate_vectors cv
SET vec_128 = (
    SELECT ARRAY(
        SELECT COALESCE(
            AVG(cas.score * ah.weight_1024_to_128),
            0.01
        )
        FROM generate_series(1, 128) AS i
        LEFT JOIN ability_hierarchy ah ON ah.atom_id_128 = (
            SELECT atom_id FROM ability_library WHERE layer = 128 ORDER BY atom_id LIMIT 1 OFFSET i-1
        )
        LEFT JOIN candidate_ability_snapshots cas
            ON cas.candidate_id = cv.candidate_id
            AND cas.atom_id = ah.atom_id_1024
            AND cas.layer = 1024
    )::VECTOR(128)
);

-- Step 4.3: 计算32维向量（按ability_hierarchy聚合）
UPDATE candidate_vectors cv
SET vec_32 = (
    SELECT ARRAY(
        SELECT COALESCE(
            AVG(cas.score * ah.weight_128_to_32),
            0.01
        )
        FROM generate_series(1, 32) AS i
        LEFT JOIN ability_hierarchy ah ON ah.atom_id_32 = (
            SELECT atom_id FROM ability_library WHERE layer = 32 ORDER BY atom_id LIMIT 1 OFFSET i-1
        )
        LEFT JOIN candidate_ability_snapshots cas
            ON cas.candidate_id = cv.candidate_id
            AND cas.atom_id = ah.atom_id_128
            AND cas.layer = 128
    )::VECTOR(32)
);

-- ============================================================================
-- Phase 5: 回填verified_skills（从现有技能提取）
-- ============================================================================

UPDATE candidate_vectors cv
SET verified_skills = (
    SELECT jsonb_agg(skill_name)
    FROM (
        SELECT DISTINCT sl.skill_name
        FROM candidate_current_skills ccs
        JOIN sub_skills sl ON ccs.sub_skill_id = sl.id
        WHERE ccs.candidate_id = cv.candidate_id
        AND ccs.level >= 5  -- 只取等级>=5的技能
        ORDER BY sl.skill_name
    ) skills
);

-- ============================================================================
-- Phase 6: 验证迁移数据完整性
-- ============================================================================

-- 检查向量覆盖率
SELECT
    'Vector Coverage' AS check_type,
    COUNT(*) AS total_candidates,
    COUNT(vec_1024) AS vec_1024_coverage,
    COUNT(vec_128) AS vec_128_coverage,
    COUNT(vec_32) AS vec_32_coverage,
    ROUND(COUNT(vec_1024) * 100.0 / COUNT(*), 2) AS vec_1024_percent
FROM candidate_vectors;

-- 检查账本追溯完整性
SELECT
    'Score Traceability' AS check_type,
    COUNT(*) AS total_scores,
    COUNT(CASE WHEN contribution_score > 0 THEN 1 END) AS scored_count,
    COUNT(CASE WHEN contribution_score = 0 THEN 1 END) AS unscored_count
FROM candidate_ability_contributions WHERE is_active = TRUE;

-- ============================================================================
-- Phase 7: 切换读路径（灰度开关）
-- ============================================================================

-- 创建灰度开关配置表
INSERT INTO system_settings (key, value, description)
VALUES (
    'search_pipeline_version',
    '{"active": "legacy", "new_table_ready": true, "migration_completed_at": "' || NOW()::TEXT || '"}'::JSONB,
    '搜索管线版本控制：legacy为旧管线，new为新管线'
);

-- 灰度切换脚本（由运维执行）
-- UPDATE system_settings
-- SET value = '{"active": "new"}'::JSONB
-- WHERE key = 'search_pipeline_version';

-- ============================================================================
-- Phase 8: 旧字段降级标记
-- ============================================================================

-- 添加降级标记注释
COMMENT ON COLUMN candidates.skill_vector IS
'【已降级】此字段为兼容保留，新系统使用candidate_vectors.vec_1024';

-- 可选：清空旧字段数据（待灰度稳定后执行）
-- UPDATE candidates SET skill_vector = NULL WHERE id IN (
--     SELECT candidate_id FROM candidate_vectors WHERE vec_1024 IS NOT NULL
-- );