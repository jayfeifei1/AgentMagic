import asyncio
import os
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastembed import TextEmbedding


MODEL_NAME = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
CACHE_DIR = os.getenv("FASTEMBED_CACHE_PATH", "/models")
embedding_model: TextEmbedding | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global embedding_model
    embedding_model = await asyncio.to_thread(
        TextEmbedding,
        model_name=MODEL_NAME,
        cache_dir=CACHE_DIR,
        local_files_only=True,
    )
    yield
    embedding_model = None


app = FastAPI(title="EchoMind Local Embedding Service", lifespan=lifespan)


class EmbedRequest(BaseModel):
    texts: List[str] = Field(min_length=1, max_length=128)


@app.get("/health")
async def health():
    if embedding_model is None:
        raise HTTPException(status_code=503, detail="BGE 模型未就绪")
    return {"status": "ok", "model": MODEL_NAME, "dimensions": 512}


@app.post("/embed")
async def embed(request: EmbedRequest):
    if embedding_model is None:
        raise HTTPException(status_code=503, detail="BGE 模型未就绪")
    vectors = await asyncio.to_thread(lambda: list(embedding_model.embed(request.texts)))
    return {"model": MODEL_NAME, "vectors": [vector.tolist() for vector in vectors]}
