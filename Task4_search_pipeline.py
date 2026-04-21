"""
L9 动态沙盒引擎 - B端混合检索管线
Task 4: search_pipeline.py

设计目标：
- 模拟B端混合搜索链路
- 实现L1-L3分层召回逻辑
- 手写实现RRF倒数秩融合算法
- Mock所有底层RPC调用
- 全链路延迟控制在模拟环境中<100ms（真实环境<1.5s）

技术栈：Python FastAPI + Mock RPC
"""

from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import asyncio
import time
import logging
import hashlib
import json

logger = logging.getLogger("L9SearchPipeline")


# ============================================================================
# 数据结构定义
# ============================================================================

@dataclass
class CandidateRecall:
    """召回候选人记录"""
    candidate_id: str
    rank: int
    similarity_score: float
    source: str  # l32/l128/l1024/sparse
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RRFResult:
    """RRF融合结果"""
    candidate_id: str
    rrf_score: float
    rank_l32: Optional[int] = None
    rank_l128: Optional[int] = None
    rank_l1024: Optional[int] = None
    rank_sparse: Optional[int] = None
    final_rank: int = 0


@dataclass
class RerankResult:
    """重排结果"""
    candidate_id: str
    rerank_score: float
    decision_reason: str
    missing_abilities: List[str] = field(default_factory=list)
    strength_abilities: List[str] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)


@dataclass
class SearchResult:
    """最终搜索结果"""
    candidate_id: str
    final_score: float
    final_rank: int
    score_breakdown: Dict[str, float]
    explanations: Dict[str, Any]
    verified_skills: List[str]
    reranker_payload: str
    radar_data: Dict[str, Any]


@dataclass
class SearchSession:
    """搜索会话"""
    session_id: str
    requirement_profile_id: str
    query_text: str
    filters: Dict[str, Any]
    results: List[SearchResult] = field(default_factory=list)
    latency_ms: int = 0
    recall_stats: Dict[str, int] = field(default_factory=dict)


# ============================================================================
# Mock RPC 函数（替代真实数据库调用）
# ============================================================================

class MockRPCClient:
    """
    Mock RPC客户端
    模拟Supabase PostgreSQL的RPC函数调用
    """

    def __init__(self):
        # Mock候选人向量库（预生成测试数据）
        self._candidate_vectors = self._generate_mock_vectors(1000)
        # Mock需求向量
        self._requirement_profiles = {}

    def _generate_mock_vectors(self, count: int) -> Dict[str, Dict]:
        """生成Mock候选人向量数据"""
        import random
        vectors = {}
        for i in range(count):
            candidate_id = f"candidate_{i:04d}"
            vectors[candidate_id] = {
                "vec_32": [random.random() for _ in range(32)],
                "vec_128": [random.random() for _ in range(128)],
                "vec_1024": [random.random() for _ in range(1024)],
                "verified_skills": random.sample(
                    ["Golang", "Python", "Java", "Redis", "PostgreSQL", "Docker",
                     "Kubernetes", "Microservices", "API Design", "Concurrent Programming"],
                    k=random.randint(2, 5)
                ),
                "reranker_payload": f"Mock candidate {i} with {random.randint(2, 5)} verified skills",
                "preferred_city": random.choice(["北京", "上海", "深圳", "杭州", "成都"]),
                "salary_min": random.randint(15, 50) * 1000,
                "is_visible": True,
                "last_certified_at": datetime.utcnow().isoformat()
            }
        return vectors

    async def search_by_vec32(
        self,
        profile_id: str,
        limit: int,
        filters: Dict[str, Any]
    ) -> List[CandidateRecall]:
        """
        Mock: L1粗召回（32维）

        模拟返回candidate_id + rank，不返回payload
        """
        logger.info(f"[MockRPC] search_by_vec32 called: profile={profile_id}, limit={limit}")

        # 获取目标向量
        target_vec = self._get_target_vec32(profile_id)

        # 计算相似度并排序（模拟HNSW索引）
        scored_candidates = []
        for candidate_id, data in self._candidate_vectors.items():
            # 应用硬过滤
            if not self._apply_filters(candidate_id, filters):
                continue

            # 计算余弦相似度（简化：欧氏距离）
            similarity = self._compute_similarity(target_vec, data["vec_32"])
            scored_candidates.append((candidate_id, similarity))

        # 排序取Top
        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        top_candidates = scored_candidates[:limit]

        return [
            CandidateRecall(
                candidate_id=candidate_id,
                rank=rank + 1,
                similarity_score=similarity,
                source="l32",
                metadata={"ttr_ms": 50}  # Mock响应时间
            )
            for rank, (candidate_id, similarity) in enumerate(top_candidates)
        ]

    async def search_by_vec128(
        self,
        profile_id: str,
        candidate_ids: List[str],
        limit: int
    ) -> List[CandidateRecall]:
        """
        Mock: L2中召回（128维）

        在L1结果集上执行，返回更精细的rank
        """
        logger.info(f"[MockRPC] search_by_vec128 called: candidates={len(candidate_ids)}, limit={limit}")

        target_vec = self._get_target_vec128(profile_id)

        scored_candidates = []
        for candidate_id in candidate_ids:
            if candidate_id not in self._candidate_vectors:
                continue

            data = self._candidate_vectors[candidate_id]
            similarity = self._compute_similarity(target_vec, data["vec_128"])
            scored_candidates.append((candidate_id, similarity))

        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        top_candidates = scored_candidates[:limit]

        return [
            CandidateRecall(
                candidate_id=candidate_id,
                rank=rank + 1,
                similarity_score=similarity,
                source="l128"
            )
            for rank, (candidate_id, similarity) in enumerate(top_candidates)
        ]

    async def search_by_vec1024(
        self,
        profile_id: str,
        candidate_ids: List[str],
        limit: int,
        must_have_atoms: List[str]
    ) -> List[CandidateRecall]:
        """
        Mock: L3精召回（1024维）

        加must_have过滤，返回最精确的rank
        """
        logger.info(f"[MockRPC] search_by_vec1024 called: candidates={len(candidate_ids)}, limit={limit}")

        target_vec = self._get_target_vec1024(profile_id)

        scored_candidates = []
        for candidate_id in candidate_ids:
            if candidate_id not in self._candidate_vectors:
                continue

            data = self._candidate_vectors[candidate_id]
            similarity = self._compute_similarity(target_vec, data["vec_1024"])

            # Mock must_have检查（简化：随机判定）
            must_have_hit = len(must_have_atoms) == 0 or random.random() > 0.2

            scored_candidates.append((candidate_id, similarity, must_have_hit))

        # 排序：must_have_hit优先，然后相似度
        scored_candidates.sort(
            key=lambda x: (x[2], x[1]),
            reverse=True
        )
        top_candidates = scored_candidates[:limit]

        return [
            CandidateRecall(
                candidate_id=candidate_id,
                rank=rank + 1,
                similarity_score=similarity,
                source="l1024",
                metadata={"must_have_hit": must_have_hit}
            )
            for rank, (candidate_id, similarity, must_have_hit) in enumerate(top_candidates)
        ]

    async def sparse_recall(
        self,
        query_tags: List[str],
        limit: int,
        filters: Dict[str, Any]
    ) -> List[CandidateRecall]:
        """
        Mock: 稀疏召回（BM25）

        对verified_skills做全文匹配
        """
        logger.info(f"[MockRPC] sparse_recall called: tags={query_tags}, limit={limit}")

        scored_candidates = []
        for candidate_id, data in self._candidate_vectors.items():
            if not self._apply_filters(candidate_id, filters):
                continue

            # 计算BM25分（简化：匹配技能数量）
            verified_skills = data["verified_skills"]
            match_count = sum(1 for tag in query_tags if tag in verified_skills)

            if match_count > 0:
                bm25_score = match_count / len(query_tags)  # 简化分数
                scored_candidates.append((candidate_id, bm25_score))

        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        top_candidates = scored_candidates[:limit]

        return [
            CandidateRecall(
                candidate_id=candidate_id,
                rank=rank + 1,
                similarity_score=score,
                source="sparse"
            )
            for rank, (candidate_id, score) in enumerate(top_candidates)
        ]

    async def get_payloads(
        self,
        candidate_ids: List[str]
    ) -> Dict[str, Dict]:
        """
        Mock: 批量获取reranker_payload

        仅在Top50后调用，减少数据传输
        """
        logger.info(f"[MockRPC] get_payloads called: candidates={len(candidate_ids)}")

        payloads = {}
        for candidate_id in candidate_ids:
            if candidate_id in self._candidate_vectors:
                data = self._candidate_vectors[candidate_id]
                payloads[candidate_id] = {
                    "verified_skills": data["verified_skills"],
                    "reranker_payload": data["reranker_payload"],
                    "radar_data": self._mock_radar_data(candidate_id)
                }

        return payloads

    # ============ 内部方法 ============

    def _get_target_vec32(self, profile_id: str) -> List[float]:
        """获取32维目标向量"""
        return [0.8, 0.7, 0.6] + [0.5] * 29  # Mock

    def _get_target_vec128(self, profile_id: str) -> List[float]:
        """获取128维目标向量"""
        return [0.85, 0.72, 0.68] + [0.5] * 125  # Mock

    def _get_target_vec1024(self, profile_id: str) -> List[float]:
        """获取1024维目标向量"""
        return [0.85, 0.72, 0.68, 0.75, 0.80] + [0.5] * 1019  # Mock

    def _compute_similarity(self, vec_a: List[float], vec_b: List[float]) -> float:
        """计算余弦相似度（Mock简化）"""
        # 简化：使用点积归一化
        dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = sum(a ** 2 for a in vec_a) ** 0.5
        norm_b = sum(b ** 2 for b in vec_b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot_product / (norm_a * norm_b)

    def _apply_filters(self, candidate_id: str, filters: Dict) -> bool:
        """应用硬过滤"""
        data = self._candidate_vectors.get(candidate_id)
        if data is None:
            return False

        # Mock过滤逻辑
        if filters.get("preferred_city") and data["preferred_city"] != filters["preferred_city"]:
            return False
        if filters.get("salary_min") and data["salary_min"] < filters["salary_min"]:
            return False
        if not data.get("is_visible", True):
            return False

        return True

    def _mock_radar_data(self, candidate_id: str) -> Dict:
        """Mock雷达图数据"""
        import random
        return {
            "layer_32": [
                {"atom_id": f"A{i:04d}", "score": random.uniform(0.5, 1.0)}
                for i in range(1, 9)
            ],
            "layer_128": [
                {"atom_id": f"A{i:04d}", "score": random.uniform(0.3, 0.9)}
                for i in range(33, 49)
            ]
        }


# ============================================================================
# RRF 倒数秩融合算法实现
# ============================================================================

def compute_rrf_score(
    ranks: Dict[str, int],
    k: int = 60,
    weights: Dict[str, float] = None
) -> float:
    """
    计算RRF倒数秩融合分数

    公式：
    RRF(d) = Σ weight_i * 1/(k + rank_i)

    Args:
        ranks: 各路的rank值 {"l32": 5, "l128": 10, "l1024": 15, "sparse": 20}
        k: 平滑常数，推荐60
        weights: 各路权重 {"l32": 0.15, "l128": 0.25, "l1024": 0.40, "sparse": 0.20}

    Returns:
        float: RRF融合分数
    """
    if weights is None:
        weights = {
            "l32": 0.15,
            "l128": 0.25,
            "l1024": 0.40,
            "sparse": 0.20
        }

    total_score = 0.0
    for source, rank in ranks.items():
        if rank is not None and rank > 0:
            weight = weights.get(source, 0.1)
            total_score += weight / (k + rank)

    return total_score


def merge_rrf_results(
    recalls: Dict[str, List[CandidateRecall]],
    k: int = 60,
    weights: Dict[str, float] = None
) -> List[RRFResult]:
    """
    RRF融合多路召回结果

    Args:
        recalls: 各路召回结果 {"l32": [...], "l128": [...], "l1024": [...], "sparse": [...]}
        k: RRF平滑常数
        weights: 各路权重配置

    Returns:
        List[RRFResult]: 融合后的候选人列表，按RRF分数排序
    """

    # 构建候选人在各路的rank映射
    candidate_ranks: Dict[str, Dict[str, int]] = {}

    for source, recall_list in recalls.items():
        for recall in recall_list:
            candidate_id = recall.candidate_id
            if candidate_id not in candidate_ranks:
                candidate_ranks[candidate_id] = {}
            candidate_ranks[candidate_id][source] = recall.rank

    # 计算每个候选人的RRF分数
    rrf_results = []
    for candidate_id, ranks in candidate_ranks.items():
        rrf_score = compute_rrf_score(ranks, k, weights)

        result = RRFResult(
            candidate_id=candidate_id,
            rrf_score=rrf_score,
            rank_l32=ranks.get("l32"),
            rank_l128=ranks.get("l128"),
            rank_l1024=ranks.get("l1024"),
            rank_sparse=ranks.get("sparse")
        )
        rrf_results.append(result)

    # 按RRF分数降序排序
    rrf_results.sort(key=lambda x: x.rrf_score, reverse=True)

    # 设置最终rank
    for rank, result in enumerate(rrf_results):
        result.final_rank = rank + 1

    logger.info(f"RRF merged {len(rrf_results)} candidates from {len(recalls)} sources")

    return rrf_results


# ============================================================================
# Mock Cross-Encoder Reranker
# ============================================================================

async def mock_rerank(
    query_text: str,
    payloads: Dict[str, Dict],
    top_k: int = 3
) -> List[RerankResult]:
    """
    Mock: Cross-Encoder重排

    模拟BGE-Reranker深度交叉注意力计算
    """
    logger.info(f"[MockRerank] reranking {len(payloads)} candidates to top {top_k}")

    import random

    results = []
    for candidate_id, payload in payloads.items():
        # Mock重排分数（基于payload与query的相关性）
        verified_skills = payload.get("verified_skills", [])
        reranker_payload = payload.get("reranker_payload", "")

        # 简化相关性计算
        relevance_score = random.uniform(0.6, 0.95)

        result = RerankResult(
            candidate_id=candidate_id,
            rerank_score=relevance_score,
            decision_reason=f"Skills {verified_skills} align with query",
            missing_abilities=random.sample(["Kafka", "GraphQL"], k=1) if random.random() > 0.7 else [],
            strength_abilities=verified_skills[:3],
            risk_flags=["技能覆盖面较窄"] if random.random() > 0.8 else []
        )
        results.append(result)

    # 按重排分数排序取Top
    results.sort(key=lambda x: x.rerank_score, reverse=True)
    return results[:top_k]


# ============================================================================
# 主搜索管线
# ============================================================================

async def execute_search_pipeline(
    session_id: str,
    requirement_profile_id: str,
    query_text: str,
    filters: Dict[str, Any],
    rpc_client: MockRPCClient,
    k_rrf: int = 60,
    weights: Dict[str, float] = None
) -> SearchSession:
    """
    执行完整的混合搜索管线

    流程：
    1. Filter Gate - 硬过滤
    2. L1 粗召回（32维） - Top 200
    3. L2 中召回（128维） - Top 80
    4. L3 精召回（1024维） - Top 40
    5. Sparse Recall Sidecar - 补漏
    6. RRF Fusion - 融合Top 50
    7. Fetch Payloads - 批量拉取
    8. Cross-Encoder Rerank - Top 3
    9. Return Results

    Returns:
        SearchSession: 搜索会话记录
    """

    start_time = time.time()

    session = SearchSession(
        session_id=session_id,
        requirement_profile_id=requirement_profile_id,
        query_text=query_text,
        filters=filters
    )

    # ========== Step 1: Filter Gate ==========
    logger.info(f"[Pipeline] Session {session_id}: Starting search pipeline")

    # ========== Step 2-5: Parallel Recall ==========
    # 从query_text提取标签（Mock）
    extracted_tags = _extract_tags_from_query(query_text)
    must_have_atoms = ["A0145", "A0042"]  # Mock必选能力

    # L1 粗召回先执行
    l32_recalls = await rpc_client.search_by_vec32(
        profile_id=requirement_profile_id,
        limit=200,
        filters=filters
    )
    session.recall_stats["l32"] = len(l32_recalls)

    # L2 在L1结果上执行
    l32_candidate_ids = [r.candidate_id for r in l32_recalls]
    l128_recalls = await rpc_client.search_by_vec128(
        profile_id=requirement_profile_id,
        candidate_ids=l32_candidate_ids,
        limit=80
    )
    session.recall_stats["l128"] = len(l128_recalls)

    # L3 在L2结果上执行
    l128_candidate_ids = [r.candidate_id for r in l128_recalls]
    l1024_recalls = await rpc_client.search_by_vec1024(
        profile_id=requirement_profile_id,
        candidate_ids=l128_candidate_ids,
        limit=40,
        must_have_atoms=must_have_atoms
    )
    session.recall_stats["l1024"] = len(l1024_recalls)

    # Sparse 并行执行（补漏）
    sparse_recalls = await rpc_client.sparse_recall(
        query_tags=extracted_tags,
        limit=100,
        filters=filters
    )
    session.recall_stats["sparse"] = len(sparse_recalls)

    # ========== Step 6: RRF Fusion ==========
    recalls = {
        "l32": l32_recalls,
        "l128": l128_recalls,
        "l1024": l1024_recalls,
        "sparse": sparse_recalls
    }

    rrf_results = merge_rrf_results(recalls, k=k_rrf, weights=weights)
    top50_rrf = rrf_results[:50]
    logger.info(f"[Pipeline] RRF fused to {len(top50_rrf)} candidates")

    # ========== Step 7: Fetch Payloads ==========
    top50_ids = [r.candidate_id for r in top50_rrf]
    payloads = await rpc_client.get_payloads(top50_ids)

    # ========== Step 8: Cross-Encoder Rerank ==========
    rerank_results = await mock_rerank(
        query_text=query_text,
        payloads=payloads,
        top_k=3
    )

    # ========== Step 9: Build Final Results ==========
    final_results = []
    for rerank in rerank_results:
        # 获取RRF信息
        rrf_info = next(
            (r for r in top50_rrf if r.candidate_id == rerank.candidate_id),
            None
        )

        # 获取payload
        payload = payloads.get(rerank.candidate_id, {})

        # 计算最终分数 = RRF * 0.6 + Rerank * 0.4
        final_score = (
            (rrf_info.rrf_score if rrf_info else 0) * 0.6 +
            rerank.rerank_score * 0.4
        )

        result = SearchResult(
            candidate_id=rerank.candidate_id,
            final_score=final_score,
            final_rank=len(final_results) + 1,
            score_breakdown={
                "rrf_score": rrf_info.rrf_score if rrf_info else 0,
                "rerank_score": rerank.rerank_score,
                "rank_l32": rrf_info.rank_l32 if rrf_info else None,
                "rank_l128": rrf_info.rank_l128 if rrf_info else None,
                "rank_l1024": rrf_info.rank_l1024 if rrf_info else None,
                "rank_sparse": rrf_info.rank_sparse if rrf_info else None
            },
            explanations={
                "decision_reason": rerank.decision_reason,
                "missing_abilities": rerank.missing_abilities,
                "strength_abilities": rerank.strength_abilities,
                "risk_flags": rerank.risk_flags
            },
            verified_skills=payload.get("verified_skills", []),
            reranker_payload=payload.get("reranker_payload", ""),
            radar_data=payload.get("radar_data", {})
        )
        final_results.append(result)

    session.results = final_results
    session.latency_ms = int((time.time() - start_time) * 1000)

    logger.info(
        f"[Pipeline] Session {session_id} completed in {session.latency_ms}ms, "
        f"returned {len(final_results)} results"
    )

    return session


def _extract_tags_from_query(query_text: str) -> List[str]:
    """Mock: 从query提取技能标签"""
    # 简化：返回常见技术关键词
    common_tags = ["Golang", "Redis", "高并发", "分布式", "死锁", "后端"]
    extracted = []
    for tag in common_tags:
        if tag.lower() in query_text.lower():
            extracted.append(tag)
    return extracted if extracted else ["Golang", "Redis"]  # 默认


# ============================================================================
# FastAPI 路由定义
# ============================================================================

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="L9 Search Pipeline API")

# 全局Mock客户端
rpc_client = MockRPCClient()


class SearchRequest(BaseModel):
    """搜索请求"""
    requirement_profile_id: str
    query_text: str
    filters: Optional[Dict[str, Any]] = None
    k_rrf: Optional[int] = 60
    weights: Optional[Dict[str, float]] = None


class SearchResponse(BaseModel):
    """搜索响应"""
    session_id: str
    results: List[Dict[str, Any]]
    latency_ms: int
    recall_stats: Dict[str, int]


@app.post("/api/search", response_model=SearchResponse)
async def search_candidates(request: SearchRequest):
    """
    B端混合搜索接口

    执行完整的L1-L3分层召回 + RRF融合 + Cross-Encoder重排
    """
    import uuid

    session_id = str(uuid.uuid4())

    session = await execute_search_pipeline(
        session_id=session_id,
        requirement_profile_id=request.requirement_profile_id,
        query_text=request.query_text,
        filters=request.filters or {},
        rpc_client=rpc_client,
        k_rrf=request.k_rrf or 60,
        weights=request.weights
    )

    return SearchResponse(
        session_id=session.session_id,
        results=[{
            "candidate_id": r.candidate_id,
            "final_score": r.final_score,
            "final_rank": r.final_rank,
            "verified_skills": r.verified_skills,
            "reranker_payload": r.reranker_payload,
            "score_breakdown": r.score_breakdown,
            "explanations": r.explanations,
            "radar_data": r.radar_data
        } for r in session.results],
        latency_ms=session.latency_ms,
        recall_stats=session.recall_stats
    )


@app.get("/api/search/session/{session_id}")
async def get_search_session(session_id: str):
    """获取搜索会话详情（用于审计）"""
    # Mock返回
    return {
        "session_id": session_id,
        "status": "completed",
        "created_at": datetime.utcnow().isoformat()
    }


# ============================================================================
# 性能测试入口
# ============================================================================

async def run_performance_test(iterations: int = 100):
    """
    性能测试

    验证模拟环境延迟 < 100ms
    """
    import uuid

    total_latency = 0
    max_latency = 0
    min_latency = float('inf')

    for i in range(iterations):
        session_id = str(uuid.uuid4())
        session = await execute_search_pipeline(
            session_id=session_id,
            requirement_profile_id="test_profile",
            query_text="需要一个能在高并发下处理Redis分布式死锁的后端，不限语言",
            filters={},
            rpc_client=rpc_client
        )

        latency = session.latency_ms
        total_latency += latency
        max_latency = max(max_latency, latency)
        min_latency = min(min_latency, latency)

    avg_latency = total_latency / iterations

    print(f"""
    ╔═════════════════════════════════════════════════╗
    ║        L9 Search Pipeline Performance Test      ║
    ╠═════════════════════════════════════════════════╣
    ║  Iterations:     {iterations}                            ║
    ║  Avg Latency:    {avg_latency:.2f}ms                        ║
    ║  Max Latency:    {max_latency}ms                           ║
    ║  Min Latency:    {min_latency}ms                           ║
    ║  Target:         < 100ms (mock) | < 1500ms (prod)          ║
    ╚═════════════════════════════════════════════════╝
    """)

    return {
        "iterations": iterations,
        "avg_latency_ms": avg_latency,
        "max_latency_ms": max_latency,
        "min_latency_ms": min_latency,
        "target_met": avg_latency < 100
    }


# ============================================================================
# 主入口
# ============================================================================

if __name__ == "__main__":
    import asyncio

    # 运行性能测试
    asyncio.run(run_performance_test(100))

    # 启动API服务
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)