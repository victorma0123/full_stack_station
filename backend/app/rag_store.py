# app/rag_store.py
from __future__ import annotations
import json, os, uuid, math
from typing import List, Dict, Tuple, Optional
import numpy as np
import requests

# —— 简易持久化 —— #
STORE_PATH = os.environ.get("RAG_STORE", "rag_store.json")
from sentence_transformers import SentenceTransformer

EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "all-MiniLM-L6-v2")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

_docs: List[Dict] = []     # [{id, title, text, chunks:[{id, text, vec(list[float])}]}]
_chunks: List[Dict] = []   # 扁平化缓存，检索时走这个

def _save():
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(_docs, f, ensure_ascii=False)

def _load():
    global _docs, _chunks
    if os.path.exists(STORE_PATH):
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            _docs = json.load(f)
    else:
        _docs = []
    _rebuild_flat()

# ✅ 可选：为 _chunks 存城市元数据，便于过滤（在 _rebuild_flat 中加入）
def _rebuild_flat():
    global _chunks
    _chunks = []
    for d in _docs:
        # 从原始 text 中粗略提取城市（也可以在 upsert_doc 扩展元数据）
        city = ""
        try:
            # 简单从 title: "[北京] 站名 (ID)" 中切
            if d.get("title","").startswith("[") and "]" in d.get("title",""):
                city = d["title"].split("]")[0].lstrip("[")
        except Exception:
            city = ""
        for c in d.get("chunks", []):
            _chunks.append({
                "doc_id": d["id"], "title": d.get("title",""),
                "chunk_id": c["id"], "text": c["text"],
                "vec": np.array(c["vec"], dtype=np.float32),
                "city": city,
            })
_st_model = None  # 全局单例（懒加载）

def _embed(texts: List[str]) -> np.ndarray:
    """用本地 Sentence-Transformers 生成向量"""
    global _st_model
    if _st_model is None:
        _st_model = SentenceTransformer(EMBED_MODEL)  # all-MiniLM-L6-v2
    vecs = _st_model.encode(texts, convert_to_numpy=True, normalize_embeddings=False)
    return np.asarray(vecs, dtype=np.float32)
def upsert_station(st: dict) -> Dict:
    """
    把一个“基站对象”作为文档写入 RAG。
    关键点：doc_id = station_id，方便检索命中后回查原始站点。
    """
    doc_id = st["id"]
    title = f"[{st.get('city','')}] {st.get('name','')} ({st['id']})"
    text  = (
        f"基站ID: {st.get('id','')}\n"
        f"城市: {st.get('city','')}\n"
        f"名称: {st.get('name','')}\n"
        f"厂商: {st.get('vendor','')}\n"
        f"频段: {st.get('band','')}\n"
        f"坐标: {st.get('lat','')},{st.get('lng','')}\n"
        f"状态: {st.get('status','')}\n\n"
        # ✅ 关键：把非结构化描述写入索引文本
        f"描述: {st.get('desc','无描述')}\n"
        f"常见问法示例: 覆盖范围、峰时负载、室内覆盖、干扰/告警、用户投诉情况等。"
    )
    return upsert_doc(title=title, text=text, doc_id=doc_id)

def upsert_station_bulk(stations: List[dict]) -> Dict:
    cnt = 0
    for st in stations:
        upsert_station(st)
        cnt += 1
    return {"ok": True, "count": cnt}


def _chunk(text: str, max_len: int = 600) -> List[str]:
    # 简单按段落/句子切块
    paras = [p.strip() for p in text.replace("\r","").split("\n\n") if p.strip()]
    chunks = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) <= max_len:
            buf = (buf + "\n\n" + p).strip()
        else:
            if buf: chunks.append(buf); buf = p
    if buf: chunks.append(buf)
    # 防止极长段落
    final = []
    for c in chunks:
        while len(c) > max_len:
            final.append(c[:max_len])
            c = c[max_len:]
        final.append(c)
    return final

def upsert_doc(title: str, text: str, doc_id: Optional[str] = None) -> Dict:
    if not doc_id:
        doc_id = str(uuid.uuid4())
    chunks = _chunk(text)
    vecs = _embed(chunks)
    doc = {"id": doc_id, "title": title, "text": text, "chunks": []}
    for i, (t, v) in enumerate(zip(chunks, vecs)):
        doc["chunks"].append({"id": f"{doc_id}:{i}", "text": t, "vec": v.tolist()})
    # 替换或新增
    existed = False
    for i, d in enumerate(_docs):
        if d["id"] == doc_id:
            _docs[i] = doc
            existed = True
            break
    if not existed:
        _docs.append(doc)
    _save()
    _rebuild_flat()
    return {"ok": True, "doc_id": doc_id, "chunks": len(chunks)}

def search(query: str, k: int = 4, *, city: Optional[str] = None, min_score: float = 0.35) -> List[Dict]:
    if not _chunks:
        return []
    qv = _embed([query])[0]
    qn = qv / (np.linalg.norm(qv) + 1e-9)

    scores: List[Tuple[int, float]] = []
    for idx, ch in enumerate(_chunks):
        if city and ch.get("city") and ch["city"] != city:
            continue  # 城市过滤
        v = ch["vec"]
        vn = v / (np.linalg.norm(v) + 1e-9)
        sim = float(np.dot(qn, vn))  # 余弦相似
        if sim >= min_score:
            scores.append((idx, sim))

    scores.sort(key=lambda x: x[1], reverse=True)
    out = []
    for idx, s in scores[:k]:
        ch = _chunks[idx]
        out.append({
            "doc_id": ch["doc_id"],
            "title": ch["title"],
            "chunk_id": ch["chunk_id"],
            "text": ch["text"],
            "score": s,
            "city": ch.get("city",""),
        })
    return out

# 初始化
_load()
