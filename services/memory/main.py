"""Memory Service - Short-term + Long-term memory with vector embeddings."""
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import faiss
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/screenmind")
FAISS_INDEX_PATH = os.getenv("FAISS_INDEX_PATH", "/data/faiss.index")
FAISS_IDS_PATH = FAISS_INDEX_PATH + ".ids"
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2 output dimension

# Global state
db_pool: Optional[asyncpg.Pool] = None
model: Optional[SentenceTransformer] = None
faiss_index: Optional[faiss.IndexFlatIP] = None
faiss_id_map: list[int] = []  # maps FAISS position -> postgres memory id


def get_or_create_faiss_index() -> faiss.IndexFlatIP:
    """Load existing FAISS index from disk or create a new one."""
    index = faiss.IndexFlatIP(EMBEDDING_DIM)  # Inner product for cosine similarity
    if os.path.exists(FAISS_INDEX_PATH):
        try:
            loaded = faiss.read_index(FAISS_INDEX_PATH)
            logger.info("Loaded FAISS index from %s (%d vectors)", FAISS_INDEX_PATH, loaded.ntotal)
            return loaded
        except Exception as exc:
            logger.warning("Could not load FAISS index: %s — creating fresh index", exc)
    return index


def load_faiss_id_map() -> list[int]:
    if os.path.exists(FAISS_IDS_PATH):
        try:
            with open(FAISS_IDS_PATH, "r") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Could not load FAISS id map: %s", exc)
    return []


def save_faiss_index():
    os.makedirs(os.path.dirname(FAISS_INDEX_PATH) or ".", exist_ok=True)
    try:
        faiss.write_index(faiss_index, FAISS_INDEX_PATH)
        with open(FAISS_IDS_PATH, "w") as f:
            json.dump(faiss_id_map, f)
        logger.debug("FAISS index persisted (%d vectors)", faiss_index.ntotal)
    except Exception as exc:
        logger.error("Failed to persist FAISS index: %s", exc)


async def init_db(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id SERIAL PRIMARY KEY,
                task_id TEXT NOT NULL,
                content TEXT NOT NULL,
                memory_type TEXT NOT NULL CHECK (memory_type IN ('short_term','long_term','failure','important_action')),
                importance_score FLOAT NOT NULL DEFAULT 0.5,
                embedding JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_task_id ON memories(task_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, model, faiss_index, faiss_id_map

    logger.info("Loading sentence-transformer model…")
    model = SentenceTransformer("all-MiniLM-L6-v2")
    logger.info("Model loaded")

    logger.info("Connecting to PostgreSQL…")
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    await init_db(db_pool)
    logger.info("Database ready")

    faiss_index = get_or_create_faiss_index()
    faiss_id_map = load_faiss_id_map()
    logger.info("FAISS index ready (%d vectors)", faiss_index.ntotal)

    yield

    await db_pool.close()
    save_faiss_index()
    logger.info("Memory service shut down")


app = FastAPI(title="Memory Service", version="1.0.0", lifespan=lifespan)


# ── Schemas ──────────────────────────────────────────────────────────────────

class StoreRequest(BaseModel):
    task_id: str
    content: str
    memory_type: str = Field(pattern="^(short_term|long_term|failure|important_action)$")
    importance_score: float = Field(default=0.5, ge=0.0, le=1.0)


class RetrieveRequest(BaseModel):
    query: str
    task_id: Optional[str] = None
    memory_type: Optional[str] = None
    top_k: int = Field(default=5, ge=1, le=50)


# ── Helpers ───────────────────────────────────────────────────────────────────

def embed(text: str) -> np.ndarray:
    vec = model.encode([text], normalize_embeddings=True)[0]
    return vec.astype(np.float32)


def row_to_dict(row) -> dict:
    d = dict(row)
    if isinstance(d.get("created_at"), datetime):
        d["created_at"] = d["created_at"].isoformat()
    if "embedding" in d:
        del d["embedding"]  # omit raw embedding from responses
    return d


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/store", status_code=201)
async def store_memory(req: StoreRequest):
    vec = embed(req.content)
    embedding_list = vec.tolist()

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memories (task_id, content, memory_type, importance_score, embedding)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            RETURNING *
            """,
            req.task_id,
            req.content,
            req.memory_type,
            req.importance_score,
            json.dumps(embedding_list),
        )

    memory_id = row["id"]

    if req.memory_type == "long_term":
        vec_2d = vec.reshape(1, -1)
        faiss_index.add(vec_2d)
        faiss_id_map.append(memory_id)
        save_faiss_index()

    logger.info("Stored memory id=%d type=%s task=%s", memory_id, req.memory_type, req.task_id)
    return row_to_dict(row)


@app.post("/retrieve")
async def retrieve_memories(req: RetrieveRequest):
    query_vec = embed(req.query)

    results = []

    if faiss_index.ntotal > 0:
        k = min(req.top_k, faiss_index.ntotal)
        query_2d = query_vec.reshape(1, -1)
        scores, indices = faiss_index.search(query_2d, k)

        candidate_ids = []
        score_map: dict[int, float] = {}
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(faiss_id_map):
                continue
            mem_id = faiss_id_map[idx]
            candidate_ids.append(mem_id)
            score_map[mem_id] = float(score)

        if candidate_ids:
            async with db_pool.acquire() as conn:
                # Build type/task filters
                conditions = ["id = ANY($1::int[])"]
                params: list = [candidate_ids]
                p = 2
                if req.task_id:
                    conditions.append(f"task_id = ${p}")
                    params.append(req.task_id)
                    p += 1
                if req.memory_type:
                    conditions.append(f"memory_type = ${p}")
                    params.append(req.memory_type)

                where = " AND ".join(conditions)
                rows = await conn.fetch(f"SELECT * FROM memories WHERE {where}", *params)

            for row in rows:
                d = row_to_dict(row)
                d["similarity_score"] = score_map.get(row["id"], 0.0)
                results.append(d)

            results.sort(key=lambda x: x["similarity_score"], reverse=True)
    else:
        # Fallback: keyword search in PostgreSQL
        logger.info("FAISS empty — falling back to keyword search")
        words = req.query.split()
        pattern = "%" + "%".join(words[:5]) + "%"  # simple prefix pattern

        async with db_pool.acquire() as conn:
            conditions = ["content ILIKE $1"]
            params: list = [pattern]
            p = 2
            if req.task_id:
                conditions.append(f"task_id = ${p}")
                params.append(req.task_id)
                p += 1
            if req.memory_type:
                conditions.append(f"memory_type = ${p}")
                params.append(req.memory_type)

            where = " AND ".join(conditions)
            rows = await conn.fetch(
                f"SELECT * FROM memories WHERE {where} ORDER BY importance_score DESC LIMIT ${p}",
                *params,
                req.top_k,
            )

        for row in rows:
            d = row_to_dict(row)
            d["similarity_score"] = None
            results.append(d)

    return {"query": req.query, "results": results, "total": len(results)}


@app.get("/memory/{task_id}")
async def get_task_memories(task_id: str):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM memories WHERE task_id = $1 ORDER BY created_at",
            task_id,
        )
    return {"task_id": task_id, "memories": [row_to_dict(r) for r in rows], "total": len(rows)}


@app.delete("/memory/{task_id}")
async def clear_short_term_memories(task_id: str):
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM memories WHERE task_id = $1 AND memory_type = 'short_term'",
            task_id,
        )
    deleted = int(result.split()[-1])
    return {"task_id": task_id, "deleted_count": deleted, "message": "Short-term memories cleared"}


@app.get("/memory/stats")
async def memory_stats():
    async with db_pool.acquire() as conn:
        type_counts = await conn.fetch(
            "SELECT memory_type, COUNT(*) as count FROM memories GROUP BY memory_type"
        )
        avg_importance = await conn.fetchrow(
            "SELECT AVG(importance_score) as avg_importance, COUNT(*) as total FROM memories"
        )

    return {
        "counts_by_type": {r["memory_type"]: r["count"] for r in type_counts},
        "total": avg_importance["total"],
        "avg_importance_score": float(avg_importance["avg_importance"] or 0),
        "faiss_index_size": faiss_index.ntotal if faiss_index else 0,
    }


@app.get("/health")
async def health():
    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_status = "ok"
    except Exception as exc:
        db_status = f"error: {exc}"

    return {
        "status": "ok",
        "database": db_status,
        "faiss_vectors": faiss_index.ntotal if faiss_index else 0,
        "model_loaded": model is not None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8008, reload=False)
