# L9 Dynamic Sandbox & Hybrid Search Pipeline

## Overview

This repository contains the deliverables for the L9 Architecture Exam - a comprehensive system design for dynamic sandbox assessment and hybrid search pipeline reconstruction.

## Deliverables

| Task | Weight | File | Description |
|-----|--------|------|-------------|
| Task 1 | 30% | `Task1_Agent_System_Prompts.md` | 4 Agent System Prompts with JSON Schema enforcement |
| Task 2 | 20% | `Task2_StateMachine_ReportSchema.md` | FSM orchestration + Certification Report Schema |
| Task 3 | 20% | `Task3_DDL_Refactor.md` | Database restructuring with HNSW indexes + migration |
| Task 4 | 30% | `Task4_search_pipeline.py` | Search pipeline with RRF algorithm |

## Key Features

- **1024 Atomic Ability Library**: Unified measurement standard
- **Three-layer Vector Architecture**: 32-dim → 128-dim → 1024-dim
- **L1-L3 Funnel Recall**: Top 200 → 80 → 40 candidates
- **RRF Fusion**: `RRF(d) = Σ weight_i/(k + rank_i)` with weights 0.15/0.25/0.40/0.20
- **FSM State Machine**: INIT → DNA_EXTRACTED → PROVISIONING → COMBAT_ACTIVE → EVALUATING → CERTIFIED/FAILED

## Tech Stack

- Python FastAPI
- Supabase PostgreSQL 15+ with pgvector
- HNSW + IVFFlat vector indexes
- BGE-Reranker (Cross-Encoder)

## Usage

```bash
# Run performance test
python Task4_search_pipeline.py

# Start API service
uvicorn Task4_search_pipeline:app --reload
```

## License

MIT