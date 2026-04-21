# L9 动态沙盒引擎 - 状态机编排与认证报告Schema
## Task 2: Agent工作流编排与报告Schema设计

---

## 一、核心状态机设计（FSM）

### 完整状态流转图

```
┌─────────────────────────────────────────────────────────────────┐
│                      L9 战役生命周期状态机                        │
└─────────────────────────────────────────────────────────────────┘

INIT ──► DNA_EXTRACTED ──► PROVISIONING ──► COMBAT_ACTIVE ──► EVALUATING
 │                          │    │              │                │ │
 │                          │    │              │                │ │
 │                          │    ▼              ▼                ▼ ▼
 │                          │ ┌─────────┐   ┌─────────┐    ┌─────────┐
 │                          │ │RETRYING │   │INTERRUPT│    │RETRYING │
 │                          │ │(重试等待)│   │(断点续传)│    │(Judge重试)│
 │                          │ └────┬────┘   └────┬────┘    └────┬────┘
 │                          │      │             │               │
 │                          ▼      ▼             ▼               ▼
 │                    ┌─────────┐           ┌─────────┐     ┌─────────┐
 │                    │ TIMEOUT │           │ RECOVERY│     │ CERTIFIED│
 │                    │(降级)   │           │ MODE    │     │ (终态)   │
 │                    └────┬────┘           └────┬────┘     └────┬────┘
 │                         │                     │                │
 │                         ▼                     ▼                ▼
 │                    FALLBACK              RESUME            REPORT
 │                    BLUEPRINT              SANDBOX           GENERATED
 │                         │                     │                │
 │                         └─────────────────────┴────────────────│
 │                                              │                 │
 │                                              ▼                 ▼
 │                                         FAILED ─────────► END
 │                                         (终态)
 │
 └─────────────────────────────────────────────────────────────────┘

【新增】RETRYING状态：Agent执行失败时的中间态，支持最多3次重试
```

### 状态定义表

| 状态 | 含义 | 进入条件 | 退出条件 | 允许的Agent |
|-----|-----|---------|---------|------------|
| `INIT` | 初始态 | 候选人选择岗位 | DNA提取请求 | - |
| `DNA_EXTRACTED` | DNA已提取 | Ingestion完成 | 战场渲染请求 | INGESTION |
| `PROVISIONING` | 框架捏造中 | Battlefield启动 | 渲染完成/超时/失败 | BATTLEFIELD |
| `RETRYING` | 重试等待中 | Agent执行失败 | 重试成功/重试耗尽 | ALL |
| `COMBAT_ACTIVE` | 沙盒对抗中 | 框架下发完成 | 候选人提交/中断/超时 | XRAG |
| `EVALUATING` | 评分计算中 | 战役结束 | Judge完成/失败 | JUDGE |
| `CERTIFIED` | 认证完成 | Judge成功 | - | - |
| `FAILED` | 认证失败 | Judge失败/重试耗尽 | - | - |
| `INTERRUPTED` | 中途中断 | 用户主动退出/网络断开 | 断点续传/放弃 | - |
| `TIMEOUT` | 超时强制结束 | 单阶段超限 | 降级/放弃 | - |

---

## 二、Python状态机伪代码实现

```python
"""
L9战役生命周期状态机
实现：基于Python枚举的有限状态机 + 异常熔断机制
"""

from enum import Enum, auto
from typing import Optional, Callable, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import asyncio
import logging

logger = logging.getLogger("L9StateMachine")


class L9State(Enum):
    """战役状态枚举"""
    INIT = auto()
    DNA_EXTRACTED = auto()
    PROVISIONING = auto()
    RETRYING = auto()          # 【新增】Agent重试中间态
    COMBAT_ACTIVE = auto()
    EVALUATING = auto()
    CERTIFIED = auto()
    FAILED = auto()
    INTERRUPTED = auto()
    TIMEOUT = auto()


class L9Exception(Exception):
    """L9引擎异常基类"""
    def __init__(self, message: str, state: L9State, recoverable: bool = False):
        super().__init__(message)
        self.state = state
        self.recoverable = recoverable


class TimeoutException(L9Exception):
    """超时异常"""
    def __init__(self, state: L9State, elapsed_seconds: float):
        super().__init__(
            f"Stage {state.name} timeout after {elapsed_seconds}s",
            state,
            recoverable=True
        )


class AgentFailureException(L9Exception):
    """Agent执行失败"""
    def __init__(self, agent_name: str, state: L9State, error_detail: str):
        super().__init__(
            f"Agent {agent_name} failed: {error_detail}",
            state,
            recoverable=True
        )


class ValidationResultException(L9Exception):
    """校验结果不合规"""
    def __init__(self, detail: str):
        super().__init__(
            f"Validation failed: {detail}",
            L9State.EVALUATING,
            recoverable=False
        )


@dataclass
class StateTransition:
    """状态转换记录"""
    from_state: L9State
    to_state: L9State
    timestamp: datetime
    trigger: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class L9SessionContext:
    """战役会话上下文"""
    session_id: str
    candidate_id: str
    assessment_id: str
    job_cert_id: str
    current_state: L9State = L9State.INIT
    state_history: list[StateTransition] = field(default_factory=list)

    # 各阶段输出
    dna_report: Optional[Dict] = None
    battlefield_manifest: Optional[Dict] = None
    battle_log: Optional[Dict] = None
    judge_result: Optional[Dict] = None

    # 时间控制
    session_started_at: datetime = field(default_factory=datetime.utcnow)
    stage_started_at: Optional[datetime] = None

    # 熔断计数
    retry_count: int = 0
    max_retries: int = 3

    # 断点续传数据
    checkpoint_data: Optional[Dict] = None


class L9StateMachine:
    """
    L9战役状态机

    设计原则：
    1. 单向流转（除INTERRUPTED和TIMEOUT可恢复）
    2. 异常熔断（超时/失败触发降级）
    3. 断点续传（INTERRUPTED支持恢复）
    4. 审计完整（所有状态变更记录在history）
    """

    # 各阶段超时配置（秒）
    STAGE_TIMEOUTS = {
        L9State.DNA_EXTRACTED: 30,      # DNA提取最长30秒
        L9State.PROVISIONING: 60,       # 框架捏造最长60秒
        L9State.COMBAT_ACTIVE: 1800,    # 沙盒对抗最长30分钟
        L9State.EVALUATING: 45,         # 评分计算最长45秒
    }

    # 状态转换规则（合法转换映射）
    VALID_TRANSITIONS: Dict[L9State, set[L9State]] = {
        L9State.INIT: {L9State.DNA_EXTRACTED},
        L9State.DNA_EXTRACTED: {L9State.PROVISIONING, L9State.FAILED},
        L9State.PROVISIONING: {L9State.COMBAT_ACTIVE, L9State.RETRYING, L9State.TIMEOUT, L9State.FAILED},
        L9State.RETRYING: {L9State.PROVISIONING, L9State.COMBAT_ACTIVE, L9State.FAILED},  # 重试后返回原状态
        L9State.TIMEOUT: {L9State.PROVISIONING, L9State.RETRYING},  # 降级或重试
        L9State.COMBAT_ACTIVE: {L9State.EVALUATING, L9State.INTERRUPTED, L9State.TIMEOUT},
        L9State.INTERRUPTED: {L9State.COMBAT_ACTIVE, L9State.FAILED},  # 断点续传
        L9State.EVALUATING: {L9State.CERTIFIED, L9State.RETRYING, L9State.FAILED},  # 评分失败可重试
        L9State.CERTIFIED: {},  # 终态
        L9State.FAILED: {},     # 终态
    }

    def __init__(
        self,
        context: L9SessionContext,
        agents: Dict[str, Callable]  # Agent函数映射
    ):
        self.ctx = context
        self.agents = agents
        self._lock = asyncio.Lock()  # 状态变更锁

    async def transition(self, to_state: L9State, trigger: str = "auto") -> bool:
        """
        状态转换

        Args:
            to_state: 目标状态
            trigger: 触发原因

        Returns:
            bool: 转换是否成功

        Raises:
            ValueError: 非法转换
        """
        async with self._lock:
            # 校验转换合法性
            if to_state not in self.VALID_TRANSITIONS[self.ctx.current_state]:
                raise ValueError(
                    f"Invalid transition: {self.ctx.current_state.name} -> {to_state.name}"
                )

            # 记录转换
            transition_record = StateTransition(
                from_state=self.ctx.current_state,
                to_state=to_state,
                timestamp=datetime.utcnow(),
                trigger=trigger,
                metadata={"retry_count": self.ctx.retry_count}
            )
            self.ctx.state_history.append(transition_record)

            # 更新状态
            old_state = self.ctx.current_state
            self.ctx.current_state = to_state
            self.ctx.stage_started_at = datetime.utcnow()

            logger.info(
                f"Session {self.ctx.session_id}: "
                f"{old_state.name} -> {to_state.name} (trigger: {trigger})"
            )

            return True

    async def check_timeout(self) -> Optional[L9Exception]:
        """检查当前阶段是否超时"""
        if self.ctx.stage_started_at is None:
            return None

        timeout_seconds = self.STAGE_TIMEOUTS.get(self.ctx.current_state)
        if timeout_seconds is None:
            return None

        elapsed = (datetime.utcnow() - self.ctx.stage_started_at).total_seconds()
        if elapsed > timeout_seconds:
            return TimeoutException(self.ctx.current_state, elapsed)

        return None

    async def handle_exception(self, exc: L9Exception) -> L9State:
        """
        异常熔断处理

        Args:
            exc: L9异常

        Returns:
            L9State: 处理后的状态
        """
        logger.error(
            f"Session {self.ctx.session_id} exception: {exc.message}",
            extra={"state": exc.state.name, "recoverable": exc.recoverable}
        )

        if exc.recoverable and self.ctx.retry_count < self.ctx.max_retries:
            # 可恢复异常，触发重试
            self.ctx.retry_count += 1

            # 【新增】进入RETRYING中间态
            await self.transition(L9State.RETRYING, trigger=f"exception_retry_{self.ctx.retry_count}")

            if isinstance(exc, TimeoutException):
                # 超时降级到Fallback Blueprint
                if exc.state == L9State.PROVISIONING:
                    await self.transition(L9State.TIMEOUT, trigger="provisioning_timeout")
                    # 触发降级逻辑
                    await self._execute_fallback_blueprint()
                    await self.transition(L9State.COMBAT_ACTIVE, trigger="fallback_deployed")
                    return L9State.COMBAT_ACTIVE

            elif isinstance(exc, AgentFailureException):
                # Agent失败，从RETRYING返回原状态重试
                await self.transition(exc.state, trigger=f"agent_retry_{self.ctx.retry_count}")
                return exc.state

        # 不可恢复或重试耗尽，进入FAILED
        await self.transition(L9State.FAILED, trigger=f"unrecoverable_{exc.message}")
        return L9State.FAILED

    async def save_checkpoint(self) -> None:
        """保存断点数据（用于INTERRUPTED恢复）"""
        self.ctx.checkpoint_data = {
            "state": self.ctx.current_state.name,
            "dna_report": self.ctx.dna_report,
            "battlefield_manifest": self.ctx.battlefield_manifest,
            "battle_log_partial": self.ctx.battle_log,
            "timestamp": datetime.utcnow().isoformat()
        }
        logger.info(f"Checkpoint saved for session {self.ctx.session_id}")

    async def recover_from_checkpoint(self) -> bool:
        """从断点恢复"""
        if self.ctx.checkpoint_data is None:
            return False

        # 恢复状态和数据
        recovered_state = L9State[self.ctx.checkpoint_data["state"]]
        if recovered_state == L9State.COMBAT_ACTIVE:
            self.ctx.dna_report = self.ctx.checkpoint_data.get("dna_report")
            self.ctx.battlefield_manifest = self.ctx.checkpoint_data.get("battlefield_manifest")
            await self.transition(L9State.COMBAT_ACTIVE, trigger="checkpoint_recovery")
            return True

        return False

    async def _execute_fallback_blueprint(self) -> None:
        """执行降级Blueprint（静态备用残卷）"""
        FALLBACK_BLUEPRINT = {
            "framework_name": "GuixinRPC-Fallback",
            "framework_version": "v1.0.0-static",
            "scenario_context": {
                "business_goal": "修复一个简单的分布式锁竞态条件",
                "constraints": ["单Redis实例", "无依赖库"],
            },
            "fatal_defects": [{
                "defect_type": "race_condition",
                "defect_location": "LockManager.acquire()",
                "defect_trigger_condition": "并发请求同一资源",
            }],
            "question_payload": {
                "starter_code": "...",  # 预存的静态代码
            }
        }
        self.ctx.battlefield_manifest = FALLBACK_BLUEPRINT
        logger.warning(f"Fallback blueprint deployed for session {self.ctx.session_id}")


# ============================================================================
# 主执行流程（Orchestration）
# ============================================================================

async def execute_l9_pipeline(
    session_id: str,
    candidate_id: str,
    assessment_id: str,
    job_cert_id: str,
    resume_text: str,
    role_schema: list[str],
    agents: Dict[str, Callable]
) -> Dict[str, Any]:
    """
    L9战役全链路执行

    Returns:
        Dict: 最终认证报告或失败信息
    """

    # 初始化上下文
    context = L9SessionContext(
        session_id=session_id,
        candidate_id=candidate_id,
        assessment_id=assessment_id,
        job_cert_id=job_cert_id
    )

    fsm = L9StateMachine(context, agents)

    try:
        # ========== Stage 1: DNA提取 ==========
        await fsm.transition(L9State.DNA_EXTRACTED, trigger="initiated")

        dna_result = await agents["ingestion"](
            resume_text=resume_text,
            role_schema=role_schema
        )

        # 校验DNA输出合规
        if "dna_report" not in dna_result:
            raise ValidationResultException("DNA output missing dna_report field")

        context.dna_report = dna_result["dna_report"]

        # ========== Stage 2: 战场渲染 ==========
        await fsm.transition(L9State.PROVISIONING, trigger="dna_complete")

        battlefield_result = await agents["battlefield"](
            candidate_dna=context.dna_report,
            role_blueprint={...}  # 从job_cert获取
        )

        context.battlefield_manifest = battlefield_result["battlefield_manifest"]

        # ========== Stage 3: 沙盒对抗 ==========
        await fsm.transition(L9State.COMBAT_ACTIVE, trigger="framework_deployed")

        # 启动沙盒监听循环
        battle_log = await run_sandbox_combat(
            fsm=fsm,
            battlefield_manifest=context.battlefield_manifest,
            xrag_agent=agents["xrag"]
        )

        context.battle_log = battle_log

        # ========== Stage 4: 谕确权 ==========
        await fsm.transition(L9State.EVALUATING, trigger="combat_complete")

        judge_result = await agents["judge"](
            role_schema=role_schema,
            battle_log=context.battle_log,
            question_ability_bindings={...}
        )

        # 校验Judge输出合规（所有atom_id必须在role_schema内）
        atom_ids = [v["atom_id"] for v in judge_result["judge_result"]["vector_updates"]]
        illegal_atoms = [a for a in atom_ids if a not in role_schema]
        if illegal_atoms:
            raise ValidationResultException(
                f"Judge generated illegal atoms: {illegal_atoms}"
            )

        context.judge_result = judge_result["judge_result"]

        # ========== Stage 5: 认证完成 ==========
        await fsm.transition(L9State.CERTIFIED, trigger="judge_complete")

        # 生成最终认证报告
        report = await generate_certification_report(context)

        return {
            "status": "CERTIFIED",
            "report": report
        }

    except L9Exception as e:
        handled_state = await fsm.handle_exception(e)
        return {
            "status": "FAILED",
            "reason": e.message,
            "final_state": handled_state.name
        }

    except Exception as e:
        # 未预期异常，直接失败
        await fsm.transition(L9State.FAILED, trigger=f"unexpected_{str(e)}")
        return {
            "status": "FAILED",
            "reason": f"Unexpected error: {str(e)}",
            "final_state": "FAILED"
        }


async def run_sandbox_combat(
    fsm: L9StateMachine,
    battlefield_manifest: Dict,
    xrag_agent: Callable
) -> Dict:
    """
    沙盒对抗执行循环

    包含：
    - WebSocket监听代码Diff
    - 定时检查触发条件
    - X-RAG攻击注入
    - 超时熔断
    """

    battle_log = {
        "code_diffs": [],
        "x_rag_responses": [],
        "runtime_reactions": [],
        "time_metrics": {}
    }

    combat_start = datetime.utcnow()
    last_diff_time = combat_start
    debounce_window = timedelta(minutes=5)  # X-RAG防抖窗口

    while True:
        # 检查超时
        timeout_exc = await fsm.check_timeout()
        if timeout_exc:
            raise timeout_exc

        # 检查中断信号
        if await check_interrupt_signal(fsm.ctx.session_id):
            await fsm.save_checkpoint()
            await fsm.transition(L9State.INTERRUPTED, trigger="user_interrupt")
            break

        # 监听代码Diff（WebSocket模拟）
        diff_event = await wait_for_code_diff(timeout=60)
        if diff_event:
            battle_log["code_diffs"].append(diff_event)
            last_diff_time = datetime.utcnow()

            # Debounce检查：只在窗口后触发X-RAG
            if (datetime.utcnow() - last_diff_time) > debounce_window:
                # 检查X-RAG触发条件
                trigger_condition = analyze_diff_for_trigger(
                    diff_event,
                    battlefield_manifest.get("x_rag_trigger_points", [])
                )

                if trigger_condition:
                    # 执行X-RAG攻击
                    xrag_attack = await xrag_agent(
                        current_state=diff_event,
                        battlefield_manifest=battlefield_manifest,
                        elapsed_time_seconds=...
                    )

                    battle_log["x_rag_responses"].append({
                        "attack": xrag_attack,
                        "candidate_response": await capture_response(),
                        "timestamp": datetime.utcnow().isoformat()
                    })

        # 检查战役结束条件
        if await check_combat_complete(battle_log):
            break

        await asyncio.sleep(5)  # 轮询间隔

    # 计算时间指标
    battle_log["time_metrics"] = {
        "total_combat_seconds": (datetime.utcnow() - combat_start).total_seconds(),
        "ttr_seconds": calculate_ttr(battle_log),
        "stuck_periods": detect_stuck_periods(battle_log)
    }

    return battle_log
```

---

## 三、极客认证报告JSON Schema

```python
"""
L9极客认证报告Schema
用于前端雷达图渲染与防伪确权
"""

from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Optional, Literal
from datetime import datetime
import hashlib
import json


# ============================================================================
# 雷达图数据结构
# ============================================================================

class RadarPoint(BaseModel):
    """雷达图单个点"""
    atom_id: str = Field(..., pattern=r"^A\d{4}$", description="原子能力ID，格式A0001-A1024")
    ability_name: str = Field(..., max_length=64)
    score: float = Field(..., ge=0.0, le=1.0, description="能力得分0-1")
    layer: Literal[32, 128, 1024] = Field(..., description="所属层级")


class RadarData(BaseModel):
    """三层雷达图数据"""
    layer_32: List[RadarPoint] = Field(default_factory=list, description="宏观层32维")
    layer_128: List[RadarPoint] = Field(default_factory=list, description="族层128维")
    layer_1024: List[RadarPoint] = Field(default_factory=list, description="原子层1024维")

    @field_validator("layer_32")
    @classmethod
    def validate_layer32_count(cls, v):
        if len(v) > 32:
            raise ValueError("layer_32 cannot exceed 32 points")
        return v

    @field_validator("layer_128")
    @classmethod
    def validate_layer128_count(cls, v):
        if len(v) > 128:
            raise ValueError("layer_128 cannot exceed 128 points")
        return v

    @field_validator("layer_1024")
    @classmethod
    def validate_layer1024_count(cls, v):
        if len(v) > 1024:
            raise ValueError("layer_1024 cannot exceed 1024 points")
        return v


# ============================================================================
# 防伪确权标识
# ============================================================================

class AntiForgery(BaseModel):
    """防伪确权数据"""
    dna_hash: str = Field(..., pattern=r"^[a-f0-9]{64}$", description="DNA快照SHA256")
    battle_log_hash: str = Field(..., pattern=r"^[a-f0-9]{64}$", description="战役日志SHA256")
    judge_signature: str = Field(..., description="Judge签名：模型版本+时间戳")
    chain_of_custody: List[Dict] = Field(
        default_factory=list,
        description="数据流转链：每个Agent处理的hash记录"
    )


# ============================================================================
# 高光时刻
# ============================================================================

class Highlight(BaseModel):
    """高光对抗切片"""
    moment_type: Literal["exception_recovery", "probing_depth", "logic_breakthrough"]
    atom_id: str
    snapshot: str = Field(..., max_length=200, description="关键时刻描述")
    score_impact: float = Field(..., ge=0.0, le=1.0)
    video_url: Optional[str] = None
    code_snippet: Optional[str] = None


# ============================================================================
# 风险标记
# ============================================================================

class RiskFlag(BaseModel):
    """风险标记"""
    flag_type: Literal["stuck_long", "no_recovery", "logic_contradiction", "timeout_exceeded"]
    atom_id: str
    severity: Literal["critical", "warning", "info"]
    suggestion: str = Field(..., max_length=200)
    occurrence_count: int = Field(default=1, ge=1)


# ============================================================================
# 战役摘要
# ============================================================================

class CombatSummary(BaseModel):
    """战役执行摘要"""
    combat_type: Literal["sandbox_code", "interview_prd"]
    framework_name: str = Field(..., max_length=50)
    framework_version: str
    total_questions: int = Field(default=1, ge=1)
    x_rag_attacks_count: int = Field(default=0, ge=0)
    successful_recoveries: int = Field(default=0, ge=0)
    stuck_periods_count: int = Field(default=0, ge=0)

    # 时间指标
    total_duration_seconds: int = Field(..., ge=0)
    ttr_seconds: Optional[int] = None
    avg_response_time_seconds: float = Field(default=0.0, ge=0.0)

    # 效率评级
    efficiency_rating: Literal["excellent", "good", "average", "needs_improvement"]


# ============================================================================
# 完整认证报告
# ============================================================================

class CertificationReport(BaseModel):
    """L9极客认证报告"""

    # 基础信息
    cert_id: str = Field(..., description="认证报告唯一ID")
    candidate_id: str = Field(..., description="候选人ID")
    assessment_id: str = Field(..., description="战役ID")
    job_cert_id: str = Field(..., description="岗位认证ID")

    # 时间戳
    combat_timestamp: datetime
    report_generated_at: datetime

    # 核心评分
    combat_confidence: float = Field(..., ge=0.0, le=1.0, description="战役置信度")
    overall_rating: Literal["L9", "L8", "L7", "L6", "L5"] = Field(
        ..., description="整体评级"
    )

    # 雷达图数据
    radar_data: RadarData

    # 验证技能
    verified_skills: List[str] = Field(
        default_factory=list,
        description="沙盒验证过的硬技能列表"
    )

    # 重排摘要（150词极限）
    reranker_payload: str = Field(..., max_length=300, description="B端重排摘要")

    # 战役摘要
    combat_summary: CombatSummary

    # 高光时刻
    highlights: List[Highlight] = Field(default_factory=list)

    # 风险标记
    risk_flags: List[RiskFlag] = Field(default_factory=list)

    # 防伪确权
    anti_forgery: AntiForgery

    # 向量快照（用于B端检索）
    vector_snapshot: Dict[str, List[float]] = Field(
        default_factory=dict,
        description="三层向量快照：vec_32, vec_128, vec_1024"
    )

    # 【新增】向量变化量（用于增量更新和重排权重调整）
    vector_delta: Optional[Dict[str, Dict[str, float]]] = Field(
        default=None,
        description="向量变化量：{atom_id: {old_score, new_score, delta}}"
    )

    # 审计元数据
    grader_model_version: str
    blueprint_version: str
    state_transition_count: int = Field(default=0, ge=0)

    @field_validator("overall_rating")
    @classmethod
    def rating_matches_confidence(cls, v, info):
        """评级与置信度一致性校验"""
        confidence = info.data.get("combat_confidence", 0)
        expected_rating = {
            (0.9, 1.0): "L9",
            (0.8, 0.9): "L8",
            (0.7, 0.8): "L7",
            (0.5, 0.7): "L6",
            (0.0, 0.5): "L5"
        }
        for (low, high), rating in expected_rating.items():
            if low <= confidence < high:
                if v != rating:
                    raise ValueError(f"Rating {v} inconsistent with confidence {confidence}")
        return v

    def compute_hash(self) -> str:
        """计算报告完整性hash"""
        payload = {
            "cert_id": self.cert_id,
            "candidate_id": self.candidate_id,
            "combat_confidence": self.combat_confidence,
            "verified_skills": sorted(self.verified_skills),
            "anti_forgery": self.anti_forgery.model_dump()
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


# ============================================================================
# Mock数据生成器（禁止硬编码）
# ============================================================================

def generate_mock_certification_report() -> CertificationReport:
    """
    生成Mock认证报告
    用于测试和演示，数据全部动态生成
    """
    import uuid
    from faker import Faker

    fake = Faker()

    # Mock雷达数据
    radar_layer32 = [
        RadarPoint(
            atom_id=f"A{i:04d}",
            ability_name=fake.job()[:20],
            score=round(fake.pyfloat(min_value=0.5, max_value=1.0), 4),
            layer=32
        )
        for i in range(1, 33)
    ]

    radar_layer128 = [
        RadarPoint(
            atom_id=f"A{i:04d}",
            ability_name=fake.word()[:15],
            score=round(fake.pyfloat(min_value=0.3, max_value=0.9), 4),
            layer=128
        )
        for i in range(33, 161)
    ]

    radar_layer1024 = [
        RadarPoint(
            atom_id=f"A{i:04d}",
            ability_name=fake.word()[:10],
            score=round(fake.pyfloat(min_value=0.0, max_value=0.85), 4),
            layer=1024
        )
        for i in range(1, 1025)
    ]

    # Mock高光
    highlights = [
        Highlight(
            moment_type="exception_recovery",
            atom_id="A0145",
            snapshot="候选人10秒内识别Redis分布式锁死锁并实现降级缓存策略",
            score_impact=0.15
        ),
        Highlight(
            moment_type="probing_depth",
            atom_id="A0042",
            snapshot="在第三层追问中准确推理资源悖论解决方案",
            score_impact=0.08
        )
    ]

    # Mock风险
    risk_flags = [
        RiskFlag(
            flag_type="stuck_long",
            atom_id="A0208",
            severity="warning",
            suggestion="并发测试环节停滞45秒，建议加强并发思维训练"
        )
    ]

    # Mock防伪
    anti_forgery = AntiForgery(
        dna_hash=hashlib.sha256(fake.text().encode()).hexdigest(),
        battle_log_hash=hashlib.sha256(fake.text().encode()).hexdigest(),
        judge_signature="ORACLE_JUDGE_v2.1.0_2026-04-21T15:30:00Z",
        chain_of_custody=[
            {"agent": "INGESTION", "hash": "..."},
            {"agent": "BATTLEFIELD", "hash": "..."},
            {"agent": "XRAG", "hash": "..."},
            {"agent": "JUDGE", "hash": "..."}
        ]
    )

    # Mock向量快照
    vector_snapshot = {
        "vec_32": [round(fake.pyfloat(min_value=0.0, max_value=1.0), 4) for _ in range(32)],
        "vec_128": [round(fake.pyfloat(min_value=0.0, max_value=1.0), 4) for _ in range(128)],
        "vec_1024": [round(fake.pyfloat(min_value=0.0, max_value=1.0), 4) for _ in range(1024)]
    }

    return CertificationReport(
        cert_id=str(uuid.uuid4()),
        candidate_id=str(uuid.uuid4()),
        assessment_id=str(uuid.uuid4()),
        job_cert_id=str(uuid.uuid4()),
        combat_timestamp=datetime.utcnow(),
        report_generated_at=datetime.utcnow(),
        combat_confidence=0.88,
        overall_rating="L8",
        radar_data=RadarData(
            layer_32=radar_layer32[:8],  # 取部分展示
            layer_128=radar_layer128[:16],
            layer_1024=radar_layer1024[:64]
        ),
        verified_skills=["Golang", "Redis", "Distributed Locks", "Concurrent Programming"],
        reranker_payload="Go沙盒战役，候选人在含Redis死锁的私有RPC框架中，准确识别竞态条件并实现降级缓存。高光：10秒内定位死锁根源。风险：并发测试停滞45秒。置信度0.88",
        combat_summary=CombatSummary(
            combat_type="sandbox_code",
            framework_name="GuixinRPC-v2.3.7",
            framework_version="v2.3.7-internal",
            x_rag_attacks_count=3,
            successful_recoveries=2,
            stuck_periods_count=1,
            total_duration_seconds=720,
            ttr_seconds=180,
            efficiency_rating="good"
        ),
        highlights=highlights,
        risk_flags=risk_flags,
        anti_forgery=anti_forgery,
        vector_snapshot=vector_snapshot,
        grader_model_version="ORACLE_JUDGE_v2.1.0",
        blueprint_version="GO_BACKEND_v1.5"
    )


# ============================================================================
# JSON输出示例
# ============================================================================

MOCK_REPORT_JSON = '''
{
  "cert_id": "550e8400-e29b-41d4-a716-446655440000",
  "candidate_id": "550e8400-e29b-41d4-a716-446655440001",
  "assessment_id": "550e8400-e29b-41d4-a716-446655440002",
  "job_cert_id": "550e8400-e29b-41d4-a716-446655440003",
  "combat_timestamp": "2026-04-21T15:00:00Z",
  "report_generated_at": "2026-04-21T15:30:00Z",
  "combat_confidence": 0.88,
  "overall_rating": "L8",
  "radar_data": {
    "layer_32": [
      {"atom_id": "A0001", "ability_name": "并发编程", "score": 0.85, "layer": 32},
      {"atom_id": "A0002", "ability_name": "分布式系统", "score": 0.72, "layer": 32},
      {"atom_id": "A0003", "ability_name": "数据库优化", "score": 0.68, "layer": 32}
    ],
    "layer_128": [
      {"atom_id": "A0033", "ability_name": "Goroutine池管理", "score": 0.85, "layer": 128},
      {"atom_id": "A0034", "ability_name": "Redis锁实现", "score": 0.72, "layer": 128}
    ],
    "layer_1024": [
      {"atom_id": "A0145", "ability_name": "分布式锁死锁识别", "score": 0.85, "layer": 1024},
      {"atom_id": "A0042", "ability_name": "高并发Goroutine同步", "score": 0.72, "layer": 1024}
    ]
  },
  "verified_skills": ["Golang", "Redis", "Distributed Locks", "Concurrent Programming"],
  "reranker_payload": "Go沙盒战役，候选人在含Redis死锁的私有RPC框架中，准确识别竞态条件并实现降级缓存。高光：10秒内定位死锁根源。风险：并发测试停滞45秒。置信度0.88",
  "combat_summary": {
    "combat_type": "sandbox_code",
    "framework_name": "GuixinRPC-v2.3.7",
    "framework_version": "v2.3.7-internal",
    "total_questions": 1,
    "x_rag_attacks_count": 3,
    "successful_recoveries": 2,
    "stuck_periods_count": 1,
    "total_duration_seconds": 720,
    "ttr_seconds": 180,
    "avg_response_time_seconds": 12.5,
    "efficiency_rating": "good"
  },
  "highlights": [
    {
      "moment_type": "exception_recovery",
      "atom_id": "A0145",
      "snapshot": "候选人10秒内识别Redis分布式锁死锁并实现降级缓存策略",
      "score_impact": 0.15
    }
  ],
  "risk_flags": [
    {
      "flag_type": "stuck_long",
      "atom_id": "A0208",
      "severity": "warning",
      "suggestion": "并发测试环节停滞45秒，建议加强并发思维训练"
    }
  ],
  "anti_forgery": {
    "dna_hash": "a1b2c3d4e5f6...",
    "battle_log_hash": "x9y8z7w6...",
    "judge_signature": "ORACLE_JUDGE_v2.1.0_2026-04-21T15:30:00Z",
    "chain_of_custody": [
      {"agent": "INGESTION", "hash": "...", "timestamp": "..."},
      {"agent": "BATTLEFIELD", "hash": "...", "timestamp": "..."},
      {"agent": "XRAG", "hash": "...", "timestamp": "..."},
      {"agent": "JUDGE", "hash": "...", "timestamp": "..."}
    ]
  },
  "vector_snapshot": {
    "vec_32": [0.85, 0.72, 0.68, ...],
    "vec_128": [0.85, 0.72, 0.68, ...],
    "vec_1024": [0.85, 0.72, 0.68, ...]
  },
  "grader_model_version": "ORACLE_JUDGE_v2.1.0",
  "blueprint_version": "GO_BACKEND_v1.5"
}
'''
```

---

## 四、前端渲染数据格式

### 雷达图配置（ECharts兼容）

```json
{
  "radar_chart_config": {
    "title": "能力雷达图 - L8认证",
    "indicator": [
      {"name": "并发编程", "max": 1},
      {"name": "分布式系统", "max": 1},
      {"name": "数据库优化", "max": 1},
      {"name": "API设计", "max": 1},
      {"name": "故障恢复", "max": 1},
      {"name": "代码质量", "max": 1}
    ],
    "series": [
      {
        "name": "本次战役",
        "type": "radar",
        "data": [
          {
            "value": [0.85, 0.72, 0.68, 0.75, 0.80, 0.78],
            "name": "候选人得分"
          },
          {
            "value": [0.90, 0.85, 0.80, 0.85, 0.90, 0.85],
            "name": "岗位要求基准"
          }
        ]
      }
    ]
  }
}
```

### 防伪标识展示

```
┌─────────────────────────────────────────────┐
│ 🔐 防伪确权标识                              │
├─────────────────────────────────────────────┤
│ DNA Hash:     a1b2c3d4e5f6...               │
│ Battle Hash:  x9y8z7w6v5u4...               │
│ Judge Sig:    ORACLE_JUDGE_v2.1.0           │
│ Chain:        INGESTION → BATTLEFIELD       │
│               → XRAG → JUDGE                │
│ Report Hash:  最终报告完整性hash            │
└─────────────────────────────────────────────┘
```