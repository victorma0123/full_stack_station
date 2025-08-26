# app/main.py
import json
import re
from typing import Any, Dict, List, AsyncGenerator
from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from app import mock_geo  # 就是上面新建的模块
from pydantic import BaseModel
from typing import Optional
from app import rag_store

# ==== Strands + Ollama（按你提供的用法）====
from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models.ollama import OllamaModel

# 连接本地 Ollama（确保 ollama serve 在跑，且已 pull 对应模型）
model = OllamaModel(
    host="http://127.0.0.1:11434",
    model_id="qwen3:1.7b",   # 改成你本机可用模型
)

agent = Agent(
    model=model,
    conversation_manager=SlidingWindowConversationManager(window_size=2),
    system_prompt="You are a helpful assistant that provides concise responses.",
    callback_handler=None,
)

# ===== 放在 main.py 顶部其它函数旁 =====
import re

FIELD_RULES = {
    "id":        [r"\b(id|编号)\b"],
    "coords":    [r"(坐标|经纬度|位置)"],
    "vendor":    [r"(厂商|vendor|供应商)"],
    "band":      [r"(频段|band)"],
    "status":    [r"(状态|online|offline|维护|maintenance)"],
    "city":      [r"(城市)"],
    "name":      [r"(名称|站名)"],
}

def _match_any(patterns: list[str], text: str) -> bool:
    for p in patterns:
        if re.search(p, text, flags=re.I):
            return True
    return False

def try_direct_answer(prompt: str, station: dict | None) -> str | None:
    """有 station 上下文时，命中简单字段就本地直答；否则返回 None。"""
    if not station:
        return None
    p = prompt.strip()

    if _match_any(FIELD_RULES["id"], p):
        return f"该基站的 ID：{station.get('id','')}"
    if _match_any(FIELD_RULES["coords"], p):
        lat, lng = station.get("lat"), station.get("lng")
        if lat is not None and lng is not None:
            return f"该基站坐标：{lat:.6f}, {lng:.6f}"
        return "该基站未提供坐标信息。"
    if _match_any(FIELD_RULES["vendor"], p):
        return f"厂商：{station.get('vendor','未知')}"
    if _match_any(FIELD_RULES["band"], p):
        return f"频段：{station.get('band','未知')}"
    if _match_any(FIELD_RULES["status"], p):
        return f"状态：{station.get('status','未知')}"
    if _match_any(FIELD_RULES["city"], p):
        return f"城市：{station.get('city','未知')}"
    if _match_any(FIELD_RULES["name"], p):
        return f"站名：{station.get('name','未知')}"

    # 很短且像“是什么/是多少”的问句，也直接用本地字段兜底
    if len(p) <= 8 and ("多少" in p or "是什么" in p):
        # 兜底优先返回最关键几项
        return (f"ID：{station.get('id','')}\n"
                f"坐标：{station.get('lat','?')}, {station.get('lng','?')}\n"
                f"厂商/频段：{station.get('vendor','?')} / {station.get('band','?')}\n"
                f"状态：{station.get('status','?')}")

    return None

app = FastAPI(title="Agent Service (Strands + Ollama)")
# --------- 地理数据：列城市 ---------
@app.get("/api/geo/cities")
def geo_cities():
    return {"ok": True, "cities": mock_geo.list_cities()}

# --------- 地理数据：列某城市的基站（随机状态）---------
@app.get("/api/geo/stations")
def geo_stations(city: str, randomize: int = 1):
    return {
        "ok": True,
        "city": city,
        "stations": mock_geo.list_stations(city, randomize_status=bool(randomize)),
    }

# --------- 地理数据：查单个基站 ---------
@app.get("/api/geo/station/{station_id}")
def geo_station_detail(station_id: str):
    s = mock_geo.get_station(station_id)
    if not s:
        return {"ok": False, "error": "station not found"}
    return {"ok": True, "station": s}

# --------- 前端“点选基站”上报（后端接收并保存到内存）---------
class SelectionIn(BaseModel):
    station_id: str
    session_id: Optional[str] = None

@app.post("/api/geo/selection")
async def geo_selection(sel: SelectionIn):
    s = await mock_geo.record_selection(sel.session_id, sel.station_id)
    if not s:
        return {"ok": False, "error": "station not found"}
    return {"ok": True, "station": s}


class UpsertIn(BaseModel):
    title: str
    text: str
    doc_id: Optional[str] = None

@app.post("/api/rag/upsert")
def rag_upsert(data: UpsertIn):
    return rag_store.upsert_doc(data.title, data.text, data.doc_id)

@app.get("/api/rag/search")
def rag_search(q: str, k: int = 4):
    return {"ok": True, "matches": rag_store.search(q, k)}
# 同步：把某城市的基站塞进 RAG（不随机状态，避免索引抖动）
@app.post("/api/rag/geo/sync")
def rag_geo_sync(city: str):
    stations = mock_geo.list_stations(city, randomize_status=False)
    res = rag_store.upsert_station_bulk(stations)  # 需在 rag_store 里有该函数
    return {"ok": True, "city": city, **res}

# 检索：自然语言搜，返回命中文档并回填 station 对象
@app.get("/api/rag/geo/search")
def rag_geo_search(q: str, k: int = 5, city: str | None = None, min_score: float = 0.35):
    hits = rag_store.search(q, k, city=city, min_score=min_score)
    for h in hits:
        st = mock_geo.get_station(h["doc_id"])
        h["station"] = st or {}
    return {"ok": True, "matches": hits}


# 允许本地前端直连
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True}

def split_think_and_final(text: str) -> tuple[str, str | None]:
    """从模型输出里分离 think（只返回安全摘要）与最终答案"""
    if not text:
        return "", None
    think = None

    # 常见样式 1: <think>...</think>
    m = re.search(r"<think>([\s\S]*?)</think>", text, re.IGNORECASE)
    if m:
        think = m.group(1)

    # 常见样式 2: ```think ... ```
    if think is None:
        m = re.search(r"```(?:thought|think|reasoning)[\s\S]*?\n([\s\S]*?)```", text, re.IGNORECASE)
        if m: think = m.group(1)

    # 常见样式 3: 显式前缀（中英）
    if think is None:
        m = re.search(r"(?:思考[:：]|推理[:：]|Thought(?:s)?[:：]|Reasoning[:：])([\s\S]{10,}?)(?:\n{1,2}(?:答案|最终答案|Answer|Final)[:：])", text, re.IGNORECASE)
        if m: think = m.group(1)

    # 去掉 think 块，得到纯正文
    cleaned = text
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"```(?:thought|think|reasoning)[\s\S]*?```", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?:思考[:：]|推理[:：]|Thought(?:s)?[:：]|Reasoning[:：])[\s\S]{10,}?(?=\n{1,2}(?:答案|最终答案|Answer|Final)[:：])", "", cleaned, flags=re.IGNORECASE)

    # 压缩空行
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    # 仅发送安全摘要（截断）
    if think:
        t = " ".join(think.split())
        think = (t[:240] + "…") if len(t) > 240 else t

    return cleaned, think


def chunk_text(text: str) -> List[str]:
    """把长文本切成小片段用于伪流式：按句号/换行/逗号分割，再限制长度。"""
    if not text:
        return []
    # 先按句读切
    parts = re.split(r'(?<=[。！？!?])|\n+', text)
    # 再做合并，保证每段不太短
    buf, out = "", []
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        if len(buf) + len(p) < 60:
            buf += p
        else:
            if buf:
                out.append(buf)
            buf = p
    if buf:
        out.append(buf)
    return out

async def sse(gen: AsyncGenerator[Dict[str, Any], None]):
    async for ev in gen:
        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

# 放到文件上方任意位置
def summarize_or_redact(text: str, limit: int = 240) -> str:
    """极简脱敏/截断：避免把完整 chain-of-thought 直出，只给摘要。"""
    if not text:
        return "（无可展示的思考摘要）"
    t = text.replace("\n", " ").strip()
    return (t[:limit] + "…") if len(t) > limit else t

def extract_safe_think_from_agent(agent) -> str:
    """示例：从 agent.messages 里尝试提取 think 字段/元数据，再做摘要。"""
    msgs = getattr(agent, "messages", []) or []
    for m in reversed(msgs):
        # 下面几种键名按你实际的 strands 结构调整
        meta = (m.get("metadata") or {}) if isinstance(m, dict) else {}
        raw = (
            meta.get("think")
            or m.get("think") if isinstance(m, dict) else None
        )
        if raw:
            return summarize_or_redact(str(raw))
    return "（无可展示的思考摘要）"

async def agent_stream(messages: List[Dict[str, str]], context: Dict[str, Any] | None = None):
    yield {"type": "start"}

    prompt = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    station = (context or {}).get("station") if isinstance(context, dict) else None

    # ★ 1) 本地直答：命中字段问答 => 不走 RAG / 模型
    direct = try_direct_answer(prompt, station)
    if direct:
        yield {"type": "log", "channel": "router", "message": "命中结构化字段，跳过RAG与模型"}
        for piece in chunk_text(direct):
            yield {"type": "token", "delta": piece}
        yield {"type": "end"}
        return

    # 可选：前端显式要求不走RAG
    prefer_no_rag = bool((context or {}).get("preferNoRag"))

    # ★ 2) 组装上下文（RAG 只做小k，且过滤/裁剪）
    rag_ctx = ""
    if not prefer_no_rag:
        matches = rag_store.search(prompt, k=2)  # 小一些，避免啰嗦与串台
        def _sanitize(t: str) -> str:
            # 过滤可能混入的长数组（类似向量打印）
            return re.sub(r"\[\s*[-\d\.\s,]{30,}\]", "[[省略]]", t)
        rag_ctx = "\n\n".join([f"[{i+1}] {_sanitize(m['text'])[:200]}" for i, m in enumerate(matches)])

    # ★ 3) 明确护栏提示：只回答“当前选中基站”，若无上下文就说不知道
    station_ctx = ""
    if station:
        station_ctx = (
            "【当前选中基站（以此为准）】\n"
            f"ID: {station.get('id','')}\n"
            f"城市: {station.get('city','')}\n"
            f"名称: {station.get('name','')}\n"
            f"厂商: {station.get('vendor','')}\n"
            f"频段: {station.get('band','')}\n"
            f"坐标: {station.get('lat','')},{station.get('lng','')}\n"
            f"状态: {station.get('status','')}\n"
        )

    guardrail = (
        "回答规则：\n"
        "1) 仅针对【当前选中基站】作答；不要引用其他基站的资料。\n"
        "2) 若资料片段与当前基站信息冲突，以当前基站信息为准。\n"
    )

    aug_prompt = (
        (station_ctx + "\n" if station_ctx else "") +
        guardrail +
        ("\n可参考（若不相关可忽略）：\n" + rag_ctx + "\n" if rag_ctx else "") +
        f"\n用户问题：{prompt}"
    )

    # ★ 4) 调模型
    text_raw = agent(aug_prompt)
    final_text, safe_think = split_think_and_final(str(text_raw))
    if safe_think:
        yield {"type": "log", "channel": "think", "message": safe_think}

    # ★ 5) 简单后验校验：若答案里出现“其它站ID”（如 ABC-123），且与当前ID不同 → 提示并纠正
    try:
        other_ids = re.findall(r"\b[A-Z]{3}-\d{3}\b", final_text)
        cur_id = (station or {}).get("id")
        if cur_id and any(x != cur_id for x in other_ids):
            yield {"type": "log", "channel": "guard", "message": f"发现疑似串台ID: {', '.join(set(other_ids))}，已提醒模型仅回答 {cur_id}"}
            # （可选）这里也可以直接把其它ID删除/替换为当前ID，或在前端提示“可能串台”
    except Exception:
        pass

    for piece in chunk_text(final_text):
        yield {"type": "token", "delta": piece}
    yield {"type": "end"}




@app.post("/api/chat/stream")
def chat_stream(payload: Dict[str, Any] = Body(...)):
    messages = payload.get("messages") or []
    return StreamingResponse(sse(agent_stream(messages)), media_type="text/event-stream")

@app.post("/api/chat")
def chat_once(payload: Dict[str, Any] = Body(...)):
    messages = payload.get("messages") or []
    prompt = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    try:
        text = agent(prompt)
        return {"ok": True, "text": str(text)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
