# -*- coding: utf-8 -*-
"""
Embedding 驱动的意图路由（替换基于正则的触发）
- 依赖 sentence-transformers 做向量化与相似度匹配
- 槽位解析用轻量规则（中文数词、m/km 归一化、编号选择），避免纯 embedding 在数值/单位上不稳
- 与现有 POI/附近流程函数做无缝对接
"""
from __future__ import annotations
from typing import Callable, Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
import math

# ===== 你项目里已有的函数/状态（按需导入/调整路径） =====
# 如果这些在 main.py 里，建议抽到 app/services 或 app/core，避免循环依赖
from app.services.nearby import (  # 假设你把它们整理到了这里；如果还在 main.py 就相对导入
    find_poi_candidates,
    filter_candidates_by_hint,
    extract_poi_key,
    parse_radius_m,              # 如果你已有这个函数可直接用；否则我们提供一个简化版
    nearby_stations_by_poi,
    _aggregate_stats,
    agent_answer_with_context,
)
from app.state import LAST_POI_STATE, _flow_expired, _clear_flow  # 你原本的全局状态与清理逻辑

# ===== 向量模型 =====
# pip install sentence-transformers
from sentence_transformers import SentenceTransformer
import numpy as np

# ----------------- 工具：中文数词与半径解析（兜底版） -----------------
_CN_NUMS = {"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}

def _cn_ordinal_to_int(text: str) -> Optional[int]:
    # “第n个/第n家/倒数第n个/最后一个/上一个/下一个” 的极简解析（可与 parse_choice_index 并存）
    t = text or ""
    if any(k in t for k in ["最后一个","最后一条","最后一家"]):
        return -1  # 特殊标记，外面再转成 n
    import re
    m = re.search(r"倒数第?\s*([一二两三四五六七八九十\d]+)", t)
    if m:
        tok = m.group(1)
        k = int(tok) if tok.isdigit() else _CN_NUMS.get(tok)
        return -k if k else None
    m = re.search(r"第\s*([一二两三四五六七八九十\d]+)", t)
    if m:
        tok = m.group(1)
        k = int(tok) if tok.isdigit() else _CN_NUMS.get(tok)
        return k if k else None
    if "上一个" in t: return 1
    if "下一个" in t: return 2
    return None

def _parse_radius_simple(text: str) -> Optional[int]:
    # 如果你已有 parse_radius_m，请直接用你自己的；这个是兜底版
    # 支持：200m / 1km / 1.5 公里 / 1500 米
    import re
    t = (text or "").replace("公尺", "米")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(m|米|km|公里)", t, flags=re.I)
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2).lower()
    if unit in ["m","米"]:
        return int(round(val))
    if unit in ["km","公里"]:
        return int(round(val * 1000))
    return None

def _norm(s: str) -> str:
    return (s or "").casefold().replace(" ", "").strip()

# ----------------- 意图定义与引擎 -----------------
@dataclass
class Intent:
    name: str
    examples: List[str]
    handler: Callable[[str], Any]
    threshold: float = 0.52  # 可按数据调
    max_ties: int = 2        # 近似并列时触发澄清

class EmbeddingRouter:
    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self.model = SentenceTransformer(model_name)
        self.intents: Dict[str, Intent] = {}
        self._emb_matrix: Optional[np.ndarray] = None
        self._emb_index_to_intent: List[str] = []

    def add_intent(self, intent: Intent):
        self.intents[intent.name] = intent
        self._rebuild_index()

    def _rebuild_index(self):
        sents, owners = [], []
        for name, it in self.intents.items():
            for ex in it.examples:
                sents.append(ex)
                owners.append(name)
        if sents:
            embs = self.model.encode(sents, normalize_embeddings=True)
            self._emb_matrix = np.array(embs, dtype=np.float32)
            self._emb_index_to_intent = owners
        else:
            self._emb_matrix = None
            self._emb_index_to_intent = []

    def _match_intent(self, text: str) -> Tuple[Optional[Intent], float, List[Tuple[str,float]]]:
        if not self._emb_matrix is not None:
            return None, 0.0, []
        q = self.model.encode([text], normalize_embeddings=True)[0]
        sims = np.dot(self._emb_matrix, q)  # 余弦相似度（归一化后点积）
        # 取每个意图的最大相似度
        best: Dict[str, float] = {}
        for sim, owner in zip(sims, self._emb_index_to_intent):
            best[owner] = max(best.get(owner, -1.0), float(sim))
        ranked = sorted(best.items(), key=lambda x: x[1], reverse=True)
        if not ranked:
            return None, 0.0, []
        top_name, top_score = ranked[0]
        it = self.intents[top_name]
        return (it if top_score >= it.threshold else None), top_score, ranked

    # 公开：主路由
    def route(self, text: str) -> Any:
        intent, score, ranked = self._match_intent(text)
        if intent is None:
            # 低置信度 → 返回一个标准澄清
            return {
                "type": "clarify_intent",
                "message": "你想找附近基站吗？可以告诉我地标/城市和半径，例如：‘西湖附近 1km 的 5G 基站’。",
                "candidates": ranked[:3],
            }
        # 处理并列（避免误判）
        ties = [x for x in ranked if x[1] >= score - 0.02]  # 2% 容忍
        if len(ties) > 1 and len(ties) <= intent.max_ties:
            # 触发一次澄清
            names = " / ".join([t[0] for t in ties])
            return {
                "type": "clarify_tie",
                "message": f"我理解到多个可能意图：{names}。请再具体一点描述你的需求？",
                "candidates": ties,
            }
        # 命中意图 → 调对应 handler
        return intent.handler(text)

# ----------------- 具体：附近基站意图 Handler -----------------
def _handle_nearby_intent(user_text: str):
    """
    复用你现有“附近流”里的 POI 召回、收敛、查询与回答逻辑，
    只是这里不再用正则触发，而是 embedding 命中后才走这段。
    """
    # 过期清理
    if _flow_expired():
        _clear_flow()

    # 1) 如果有待选，先尝试用文本选择（编号/中文序数/名字子串）
    if LAST_POI_STATE.get("candidates"):
        cands = LAST_POI_STATE["candidates"]

        # —— 选择解析（不依赖 regex 的极简版）——
        # a) 纯数字
        stripped = (user_text or "").strip()
        chosen = None
        if stripped.isdigit():
            idx = int(stripped)
            if 1 <= idx <= len(cands):
                chosen = cands[idx-1]
        else:
            # b) 中文“第N个/倒数第N个/最后一个…”
            ordk = _cn_ordinal_to_int(user_text)
            if ordk:
                if ordk == -1:
                    chosen = cands[-1]
                elif ordk < 0:
                    k = -ordk
                    if 1 <= k <= len(cands):
                        chosen = cands[-k]
                elif 1 <= ordk <= len(cands):
                    chosen = cands[ordk-1]
            # c) 名称子串
            if not chosen:
                nt = _norm(user_text)
                hits = [x for x in cands if _norm(x.get("name","")) and nt in _norm(x.get("name",""))]
                if len(hits) == 1:
                    chosen = hits[0]

        narrowed = [chosen] if chosen else filter_candidates_by_hint(cands, user_text)

        if len(narrowed) == 1:
            poi = narrowed[0]
            LAST_POI_STATE.update({"selected": poi, "candidates": [], "city_hint": poi.get("city")})
            radius = (parse_radius_m(user_text) or _parse_radius_simple(user_text) 
                      or int(poi.get("radius_m") or 1000))
            hits = nearby_stations_by_poi(poi, radius_m=radius) or []
            ctx = {
                "poi": {
                    "id": poi.get("id"), "name": poi.get("name"),
                    "city": poi.get("city"), "district": poi.get("district"),
                    "addr_hint": poi.get("addr_hint"), "lat": poi.get("lat"), "lng": poi.get("lng"),
                    "radius_m": radius
                },
                "summary": _aggregate_stats(hits),
                "representatives": [
                    {k: r.get(k) for k in ("id","name","vendor","band","status","_dist_m","lat","lng")}
                    for r in hits[:8]
                ]
            }
            # 这里直接返回结构，或沿用你项目中 agent 的事件流形式
            return {"type": "nearby_result", "context": ctx}
        else:
            # 继续追问（不回显清单）
            return {
                "type":"clarify_poi",
                "message":"有多个同名地标，请补充城市/区县或更具体地址（也可直接说第几个）。"
            }

    # 2) 首问：召回候选
    cands, city_hint = find_poi_candidates(user_text)
    if not cands:
        return {
            "type":"clarify_poi",
            "message":"没找到对应地标，请补充“城市/区县 + 更具体地标 + 半径（如 1km）”。"
        }

    narrowed = filter_candidates_by_hint(cands, user_text) if cands else []
    if len(narrowed) == 1:
        poi = narrowed[0]
        LAST_POI_STATE.update({"selected": poi, "candidates": [], "city_hint": city_hint or poi.get("city")})
        radius = (parse_radius_m(user_text) or _parse_radius_simple(user_text) 
                  or int(poi.get("radius_m") or 1000))
        hits = nearby_stations_by_poi(poi, radius_m=radius) or []
        ctx = {
            "poi": {
                "id": poi.get("id"), "name": poi.get("name"),
                "city": poi.get("city"), "district": poi.get("district"),
                "addr_hint": poi.get("addr_hint"), "lat": poi.get("lat"), "lng": poi.get("lng"),
                "radius_m": radius
            },
            "summary": _aggregate_stats(hits),
            "representatives": [
                {k: r.get(k) for k in ("id","name","vendor","band","status","_dist_m","lat","lng")}
                for r in hits[:8]
            ]
        }
        return {"type": "nearby_result", "context": ctx}

    # 3) 多候选：进入待选
    LAST_POI_STATE.update({"candidates": narrowed or cands, "selected": None, "city_hint": city_hint})
    return {
        "type":"clarify_poi",
        "message":"我找到了多个可能的地标，请补充城市/区县或直接告诉我编号/名字的一部分。"
    }

# ----------------- 工厂：创建路由器并注册意图 -----------------
def build_router() -> EmbeddingRouter:
    er = EmbeddingRouter()
    er.add_intent(Intent(
        name="nearby_stations",
        examples=[
            "西湖附近有哪些5G基站",
            "帮我查人民广场周边 4G/5G 基站",
            "我在广州塔，1公里内有哪些运营商基站",
            "附近基站覆盖怎么样",
            "周边有没有电信5G站",
            "这里的 5G 基站多不多",
            "附近移动/联通/电信基站",
        ],
        handler=_handle_nearby_intent,
        threshold=0.50
    ))
    # 未来可继续 add_intent(...) 注册更多意图
    return er

# ----------------- FastAPI 适配（示例） -----------------
# 你可以把这个 APIRouter 暴露出去，在 main.py 里 include_router
from fastapi import APIRouter
from pydantic import BaseModel

class RouteIn(BaseModel):
    text: str

router = APIRouter(prefix="/embed-router", tags=["embed-router"])
_engine = build_router()

@router.post("/route")
def route_text(inp: RouteIn):
    result = _engine.route(inp.text)
    return result
