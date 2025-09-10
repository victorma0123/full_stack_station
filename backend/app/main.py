

# app/main.py
import json
from typing import Any, Dict, List, AsyncGenerator
from fastapi import FastAPI, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from app import mock_geo  # 就是上面新建的模块
from app import db_json
from pydantic import BaseModel
from typing import Optional
import anyio
from fastapi import Request
import base64
import httpx
import asyncio
from collections import Counter


#from app import rag_store

# ==== Strands + Ollama（按你提供的用法）====
from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models.ollama import OllamaModel
import hashlib
from math import isnan

# === 图表解读小工具 ===
def _aggregate_stats(rows: list[dict]) -> dict:
    from collections import Counter
    import math, statistics as st
    vendors = Counter([(r.get("vendor") or "未知") for r in rows])
    statuses = Counter([(r.get("status") or "未知").lower() for r in rows])
    bands = Counter([(r.get("band") or "未知") for r in rows])
    times = [int(r.get("updated_at") or 0) for r in rows if r.get("updated_at")]
    hist = None
    if times:
        times_sorted = sorted(times)
        hist = {
            "min": times_sorted[0],
            "p50": times_sorted[len(times_sorted)//2],
            "max": times_sorted[-1],
            "mean": int(st.mean(times)),
        }
    top_vendor = vendors.most_common(1)[0][0] if vendors else None
    return {
        "n": len(rows),
        "vendor_counts": dict(vendors),
        "status_counts": dict(statuses),
        "band_counts": dict(bands),
        "updated_at_summary": hist,
        "top_vendor": top_vendor,
    }

def _classify_kind(prompt: str) -> str:
    p = prompt or ""
    if re.search(r"(甜甜圈|donut)", p, re.I): return "donut"
    if re.search(r"(饼图|pie)", p, re.I): return "pie"
    if re.search(r"(热力|heatmap)", p, re.I): return "heatmap"
    if re.search(r"(堆叠|stack)", p, re.I): return "stacked"
    if re.search(r"(直方|hist)", p, re.I): return "hist"
    if re.search(r"(水平|horizontal|barh|hbar)", p, re.I): return "horizontal"
    return "bar"  # 默认柱状


# 连接本地 Ollama（确保 ollama serve 在跑，且已 pull 对应模型）
model = OllamaModel(
    host="http://127.0.0.1:11434",
    model_id="qwen3:1.7b",   # 改成你本机可用模型
)
OLLAMA_HOST = "http://127.0.0.1:11434"
OLLAMA_MODEL_ID = "qwen3:1.7b"


agent = Agent(
    model=model,
    conversation_manager=SlidingWindowConversationManager(window_size=2),
    system_prompt="You are a helpful assistant that provides concise responses.",
    callback_handler=None,
)

# ===== 放在 main.py 顶部其它函数旁 =====
import re
from . import chart_specs


async def stream_from_ollama(prompt: str):
    """
    直接对接 Ollama /api/generate 的流式接口：
    一行一个 JSON：{"response": "...", "done": false} ... {"done": true}
    """
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model": OLLAMA_MODEL_ID,
                "prompt": prompt,
                "stream": True,
            },
            headers={"Accept": "application/json"},
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if "response" in obj:
                    # 这里返回“增量”
                    yield obj["response"]
                if obj.get("done"):
                    break

# 1) 新增：按“站名/名称/叫/引号”提取名字，并用本地库解析成 station
NAME_HINT_RE = re.compile(r"(?:站名|名称|名字|名为|叫)\s*([^\s，。,:;!?【】《》]{2,32})")
QUOTED_NAME_RE = re.compile(r"[“\"']([^“\"']{2,32})[”\"']")

def extract_station_name(prompt: str) -> str | None:
    if not prompt: return None
    m = QUOTED_NAME_RE.search(prompt) or NAME_HINT_RE.search(prompt)
    if m: return m.group(1)
    # 兜底：匹配“城市-示例站数字”这类常见命名
    m = re.search(r"[\u4e00-\u9fff]{2,8}-?示例站\d{1,3}", prompt)
    return m.group(0) if m else None

def resolve_station_from_prompt(prompt: str) -> dict | None:
    # 先按 ID
    sid = extract_station_id(prompt or "")
    if sid:
        s = db_json.get_station(sid)
        if s: return s
    # 再按名字（可结合城市缩小范围）
    name = extract_station_name(prompt or "")
    if not name:
        # 没有明确名字，就用现有 TopK 逻辑挑一个强相关候选（避免瞎猜）
        topk = topk_context_for_prompt(prompt, k=1)
        return topk[0] if topk else None
    city = extract_city(prompt or "")
    items = db_json.load_all()
    cand = [s for s in items if (not city or s.get("city")==city)]
    def score(s):
        n = s.get("name","")
        sc = 0
        if name in n: sc += 5
        if n in name: sc += 4
        # 轻量 token 命中
        for t in re.findall(r"[\u4e00-\u9fffA-Za-z0-9\-]+", prompt or ""):
            if t and t in n: sc += 1
        return sc + (1 if city and s.get("city")==city else 0)
    if not cand: return None
    best = max(cand, key=score)
    return best if score(best) >= 2 else None  # 阈值防误判

STATUS_ALIASES = {
    "online": ["online", "在线", "在网", "上线"],
    "maintenance": ["maintenance", "维护", "检修", "保养"],
    "offline": ["offline", "离线", "下线", "停机"],
}
def normalize_status(s: str) -> str | None:
    s = (s or "").lower()
    for k, vs in STATUS_ALIASES.items():
        if any(v.lower() in s for v in vs):
            return k
    return None

def extract_city_status_count(prompt: str):
    """
    解析：'上海几个是online的' / '北京在线有多少个' / '杭州维护的有几站' 等
    返回: (city, status) 或 None
    """
    if not prompt:
        return None
    city = extract_city(prompt)
    if not city:
        return None

    # 统一状态词
    STATUS_WORDS = r"(在线|离线|维护|online|offline|maintenance)"
    # 两种顺序：① 先状态后“几个/多少/几”；② 先“几个/多少/几”后状态
    pat = re.compile(
        rf"(?:(?P<status1>{STATUS_WORDS}).{{0,8}}?(?:几个|多少|几))|(?:(?:几个|多少|几).{{0,8}}?(?P<status2>{STATUS_WORDS}))",
        re.IGNORECASE,
    )
    m = pat.search(prompt)
    if not m:
        return None
    status_raw = m.group("status1") or m.group("status2")
    status = normalize_status(status_raw)
    if not status:
        return None
    return city, status

# --- 轻规则 & Markdown 工具 ---

CITY_NAMES = ["北京","上海","广州","深圳","杭州"]

def extract_city(prompt: str) -> str | None:
    for c in CITY_NAMES:
        if c in prompt:
            return c
    return None

def extract_station_id(prompt: str) -> str | None:
    m = re.search(r"\b([A-Za-z]{2,5})-?\s*(\d{2,6})\b", prompt or "", re.I)
    if not m:
        return None
    prefix = m.group(1).upper()
    num = m.group(2).zfill(3)  # 我们数据是 3 位，如需兼容更多可放宽
    return f"{prefix}-{num}"


# 放在 main.py 里（或你定义 want_list 的地方）
LIST_HINT = ["有哪些", "都有什么", "列出", "清单", "罗列", "list", "所有", "全部"]
VIS_HINT_RE = re.compile(r"(出图|图表|可视化|柱状|折线|饼图|plot|chart|bar)", re.I)

def want_list(prompt: str) -> bool:
    p = prompt or ""
    if VIS_HINT_RE.search(p):              # 有可视化意图 → 不走清单
        return False
    has_city = extract_city(p) is not None
    listy = any(h in p for h in LIST_HINT)
    # 恢复“城市+基站”的兜底（且不含可视化意图时）
    return listy or (has_city and "基站" in p)



def station_to_markdown(st: dict) -> str:
    if not st: return "未找到该基站。"
    lat, lng = st.get("lat"), st.get("lng")
    lines = [
        f"### {st.get('name','未知')}（{st.get('id','')}）",
        "",
        "| 字段 | 值 |",
        "|---|---|",
        f"| 城市 | {st.get('city','')} |",
        f"| 厂商 | {st.get('vendor','')} |",
        f"| 频段 | {st.get('band','')} |",
        f"| 状态 | {st.get('status','')} |",
        f"| 坐标 | {lat}, {lng} |",
    ]
    if st.get("desc"):
        lines += ["", f"> 备注：{st['desc']}"]
    if lat is not None and lng is not None:
        osm = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lng}#map=16/{lat}/{lng}"
        lines += ["", f"[在 OpenStreetMap 查看]({osm})"]
    return "\n".join(lines)
def _md_table(header_cols, rows):
    head = "| " + " | ".join(header_cols) + " |"
    sep  = "|" + "|".join(["---"] * len(header_cols)) + "|"
    body = ["| " + " | ".join(map(str, r)) + " |" for r in rows]
    return "\n".join([head, sep, *body])

def _breakdown_table(title, counter: Counter, topn: int = 6):
    items = counter.most_common(topn)
    md = f"**{title}**\n\n" + _md_table(["项", "数量"], items if items else [["—", 0]])
    return md

def render_city_overview_report(city: str, rows: list[dict]) -> str:
    total = len(rows)
    status_ct = Counter([r.get("status","").lower() for r in rows])
    vendor_ct = Counter([r.get("vendor","") for r in rows])
    band_ct   = Counter([r.get("band","") for r in rows])

    # 1) 概览
    p1 = [
        f"# 1. 概览",
        f"- **城市**：{city}",
        f"- **基站总数**：**{total}**\n",
        f"- **状态分布**：在线 **{status_ct.get('online',0)}** · 维护 **{status_ct.get('maintenance',0)}** · 离线 **{status_ct.get('offline',0)}**",
    ]

    # 2) 网络情况分析
    p2 = [
        f"# 2. 网络情况分析",
        "- **重点**：关注**离线**与**维护**站点的成因（电源/回传/射频），以及高负荷小区的扩容计划。\n",
        _breakdown_table("厂商分布", vendor_ct),
        "",
        _breakdown_table("频段分布", band_ct),
    ]

    # 3) 数据明细（表格）
    detail_rows = [
        [r["id"], r["name"], r["vendor"], r["band"], r["status"]]
        for r in rows[:100]  # 明细最多前 100 条，防止过长
    ]
    p3 = [
        f"# 3. 数据明细",
        _md_table(["ID", "名称", "厂商", "频段", "状态"], detail_rows if detail_rows else [["—","—","—","—","—"]]),
    ]

    # 4) 路由/管理检查（示例建议 & 等宽高亮）
    p4 = [
        f"# 4. 路由与管理检查（建议）",
        "- **OSPF** 邻接与收敛时延抽样；**SNMP** 采样完整性；NTP 偏移监控。",
        "- 样例核查项：",
        "  - · `router-id 1.1.1.1` 是否统一规范",
        "  - · `LLDP` 拓扑邻接是否闭环",
        "  - · 回传口 QOS/ACL 是否与基线一致（如 `tangro` 模板）",
    ]
    return "\n\n".join(["\n".join(p1), "\n".join(p2), "\n".join(p3), "\n".join(p4)])

def render_city_status_report(city: str, status: str, rows: list[dict]) -> str:
    total = len(rows)
    vendor_ct = Counter([r.get("vendor","") for r in rows])
    band_ct   = Counter([r.get("band","") for r in rows])

    p1 = [
        f"# 1. 概览",
        f"- **城市**：{city}",
        f"- **状态**：**{status}**",
        f"- **基站数量**：**{total}**",
    ]

    p2 = [
        f"# 2. 网络情况分析",
        "- **重点**：若为 **offline**，优先排查电源/传输；若为 **maintenance**，关注工单进度与风险窗口；若为 **online**，抽样 KPI。",
        _breakdown_table("厂商分布", vendor_ct),
        "",
        _breakdown_table("频段分布", band_ct),
    ]

    detail_rows = [
        [r["id"], r["name"], r["vendor"], r["band"]]
        for r in rows[:60]
    ]
    p3 = [
        f"# 3. 数据明细",
        _md_table(["ID", "名称", "厂商", "频段"], detail_rows if detail_rows else [["—","—","—","—"]]),
    ]

    p4 = [
        f"# 4. 路由检查（示例）",
        "- 核查要点：",
        "  - · **OSPF** 邻接是否稳定，LSA 泛洪是否异常",
        "  - · **BFD** 是否启用，故障切换是否在目标时延内",
        "  - · `snmp-server community public RO` 等敏感配置是否符合安全基线",
    ]
    return "\n\n".join(["\n".join(p1), "\n".join(p2), "\n".join(p3), "\n".join(p4)])

def city_table_markdown(city: str, rows: list[dict]) -> str:
    """把某个城市的基站列表转成 Markdown 报告格式"""

    total = len(rows)
    if not rows:
        return f"# {city} 基站清单\n\n⚠️ 没有找到相关基站。\n"

    # 概览部分
    parts = [
        f"# {city} 基站清单\n",
        f"**城市**：{city}  \n**基站总数**：**{total}**\n",
        "---\n",  # 分隔线
        "## 数据明细\n",
    ]

    # 表格标题
    header = ["ID", "名称", "厂商", "频段", "状态"]
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    # 内容行
    for r in rows:
        line = "| " + " | ".join([
            str(r.get("id", "—")),
            str(r.get("name", "—")),
            str(r.get("vendor", "—")),
            str(r.get("band", "—")),
            f"**{r.get('status', '—')}**",   # 状态加粗
        ]) + " |"
        lines.append(line)

    parts.append("\n".join(lines))
    parts.append("\n---\n")  # 结尾分隔线

    return "\n".join(parts)


def topk_context_for_prompt(prompt: str, k: int = 12) -> list[dict]:
    """复用 /api/db/stations/search 的简易打分逻辑，供模型兜底拼上下文。"""
    items = db_json.load_all()
    terms = [t for t in re.split(r"\s+", prompt or "") if t]
    def ci(s): return str(s or "").lower()
    def hit(st, term: str) -> bool:
        t = term.lower()
        return (
            t in ci(st.get("id")) or
            t in ci(st.get("name")) or
            t in ci(st.get("city")) or
            t in ci(st.get("vendor")) or
            t in ci(st.get("band")) or
            t in ci(st.get("status")) or
            t in ci(st.get("desc"))
        )
    scored = []
    for st in items:
        score = sum(1 for t in terms if hit(st, t)) if terms else 0
        if terms and score == 0:
            continue
        score += 0.1 * ((st.get("updated_at") or 0) / 1e12)
        scored.append((score, st))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:k]]

def rows_to_compact_md(rows: list[dict]) -> str:
    if not rows: return ""
    head = "| ID | 城市 | 名称 | 厂商 | 频段 | 状态 | lat | lng |"
    sep  = "|---|---|---|---|---|---|---|---|"
    body = [f"| {r.get('id','')} | {r.get('city','')} | {r.get('name','')} | {r.get('vendor','')} | {r.get('band','')} | {r.get('status','')} | {r.get('lat','')} | {r.get('lng','')} |" for r in rows]
    return "\n".join([head, sep, *body])

FIELD_RULES = {
    "id":        [r"\b(id|编号)\b"],
    "coords":    [r"(坐标|经纬度|位置)"],
    "vendor":    [r"(厂商|vendor|供应商)"],
    "band":      [r"(频段|band)"],
    "status":    [r"(状态|online|offline|维护|maintenance)"],
    "city":      [r"(城市)"],
    "name":      [r"(名称|站名)"],
}
BAND_RADIUS_M = {
    "n78": (300, 800),
    "n41": (500, 1200),
    "n1":  (800, 2000),
    "n28": (1500, 5000),
}


def _seed_all():
    cities = ["北京","上海","广州","深圳","杭州"]
    out = []
    for c in cities:
        out.extend(mock_geo.list_stations(c, randomize_status=False))
    return out
db_json.init_if_missing(_seed_all())
def _match_any(patterns: list[str], text: str) -> bool:
    for p in patterns:
        if re.search(p, text, flags=re.I):
            return True
    return False
def _stable_jitter(key: str, low: int, high: int, jitter: float = 0.15) -> int:
    """用 station_id+band 生成稳定抖动，避免每次重启都变"""
    h = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16)
    base = (low + high) // 2
    span = int(base * jitter)
    return max(low, min(high, base + (h % (2*span+1) - span)))

def estimate_coverage_radius_m(station: dict) -> int:
    band = (station.get("band") or "").lower()
    rng = BAND_RADIUS_M.get(band, (600, 1200))
    r = _stable_jitter(f"{station.get('id','')}|{band}", rng[0], rng[1], jitter=0.18)

    status = (station.get("status") or "").lower()
    if status == "offline":
        return 0
    if status == "maintenance":
        r = int(r * 0.7)

    desc = (station.get("desc") or "")
    if any(k in desc for k in ("写字楼", "地铁", "商场")):
        r = int(r * 0.9)
    if any(k in desc for k in ("居民区", "公园", "绿地")):
        r = int(r * 1.05)

    return max(0, r)

def reverse_geocode(lat: float, lng: float) -> str | None:
    """占位：当前不走外部API。后续可接 Nominatim/高德/Google 并加缓存。"""
    try:
        if lat is None or lng is None or isnan(float(lat)) or isnan(float(lng)):
            return None
        return None
    except Exception:
        return None

FIELD_RULES.update({
    "detail": [r"(细节|详情|信息|概况|简介|介绍|明细|详细|情况)"],
})
def try_direct_answer(prompt: str, station: dict | None) -> str | None:
    """有 station 上下文时，命中简单字段就本地直答；否则返回 None。"""
    if not station:
        return None
    p = prompt.strip()
    if _match_any(FIELD_RULES["detail"], p):
        return station_to_markdown(station)

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
def geo_stations(city: str):
    stations = db_json.search_stations(city=city, limit=500)
    return {"ok": True, "city": city, "stations": stations}

# --------- 地理数据：查单个基站 ---------
@app.get("/api/geo/station/{station_id}")
def geo_station_detail(station_id: str):
    s = db_json.get_station(station_id)
    if not s:
        return {"ok": False, "error": "station not found"}
    return {"ok": True, "station": s}

# --------- 前端“点选基站”上报（后端接收并保存到内存）---------
class SelectionIn(BaseModel):
    station_id: str
    session_id: Optional[str] = None

# --------- 覆盖估算：返回点位、半径与可读地址（可为空）---------
@app.get("/api/geo/coverage")
def geo_coverage(station_id: str):
    s = db_json.get_station(station_id)
    if not s:
        return {"ok": False, "error": "station not found"}

    lat, lng = s.get("lat"), s.get("lng")
    r = estimate_coverage_radius_m(s)
    addr = reverse_geocode(lat, lng)

    # 简化：前端画 Circle 即可，这里不生成 Polygon
    return {
        "ok": True,
        "station": {
            "id": s.get("id"),
            "name": s.get("name"),
            "city": s.get("city"),
            "lat": lat, "lng": lng,
            "band": s.get("band"),
            "vendor": s.get("vendor"),
            "status": s.get("status"),
            "updated_at": s.get("updated_at"),
        },
        "address": addr,
        "radius_m": r,
        "meta": {"confidence": 0.6 if r>0 else 0.0, "source": "heuristic"},
    }

@app.post("/api/geo/selection")
async def geo_selection(sel: SelectionIn):
    # 保留选择记忆的语义——直接回传 station 即可（如需跨会话记忆可继续用 mock_geo 的内存映射）
    s = db_json.get_station(sel.station_id)
    if not s:
        return {"ok": False, "error": "station not found"}
    return {"ok": True, "station": s}


@app.get("/api/db/stations/search")
def db_stations_search(
    q: Optional[str] = None,
    vendor: Optional[str] = None,
    band: Optional[str] = None,
    status: Optional[str] = None,
    k: int = Query(20, le=200),
):
    """
    全量多字段检索（不限定城市）：
    - q 会在 id/name/city/vendor/band/status/desc 上做不区分大小写的包含匹配
    - 支持 vendor/band/status 作为精确过滤（可选）
    - 简单相关性：命中字段越多分数越高
    """
    items = db_json.load_all()
    if not q and not any([vendor, band, status]):
        # 没有任何条件就给最新的前 k 条
        items = sorted(items, key=lambda x: x.get("updated_at") or 0, reverse=True)[:k]
        return {"ok": True, "matches": items}

    terms = [t for t in re.split(r"\s", q or "") if t]
    def ci(s): return str(s or "").lower()
    def hit(st, term: str) -> bool:
        t = term.lower()
        return (
            t in ci(st.get("id")) or
            t in ci(st.get("name")) or
            t in ci(st.get("city")) or
            t in ci(st.get("vendor")) or
            t in ci(st.get("band")) or
            t in ci(st.get("status")) or
            t in ci(st.get("desc"))
        )
    # 结构化过滤（可选）
    def pass_filters(st):
        if vendor and ci(st.get("vendor")) != ci(vendor): return False
        if band   and ci(st.get("band"))   != ci(band):   return False
        if status and ci(st.get("status")) != ci(status): return False
        return True
    # 评分：每个 term 命中一个字段 1，命中多个字段 多分
    scored = []
    for st in items:
        if not pass_filters(st): 
            continue
        if not terms:
            score = 0
        else:
            score = sum(1 for t in terms if hit(st, t))
            if score == 0:
                continue
        # 轻微最近性加权
        score += 0.1 * ((st.get("updated_at") or 0) / 1e12)  # 很小的加权，避免影响主相关性
        scored.append((score, st))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = [s for _, s in scored[:k]]
    return {"ok": True, "matches": out}


# 允许本地前端直连
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://10.50.13.148:3000",  # ← 你的 Dev 机 IP
    ],
    # 或者用正则（FastAPI 也支持）：
    # allow_origin_regex=r"http://.*:3000",
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
async def heartbeat(interval: float = 10.0):
    """SSE 心跳：注释行会刷新代理/浏览器缓冲"""
    while True:
        yield ": ping\n\n"
        await anyio.sleep(interval)

@app.get("/api/chat/sse")
async def chat_sse(payload: str = Query(...)):
    """
    EventSource 使用的 GET SSE 入口。
    前端会把 {messages, context} 打包成 base64 放到 ?payload=
    """
    # 1) 解析 payload
    try:
        raw = base64.b64decode(payload.encode("utf-8")).decode("utf-8")
        data = json.loads(raw)
        messages = data.get("messages") or []
        ctx = data.get("context") or None
    except Exception as e:
        # 出错也要用 SSE 格式回一条错误，再 end
        async def err_gen():
            yield f"data: {json.dumps({'type':'token','delta': f'参数解析失败：{e}'}, ensure_ascii=False)}\n\n"
            yield "data: {\"type\":\"end\"}\n\n"
        return StreamingResponse(
            err_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # 2) 合并“模型输出流”和“心跳流”
    async def merged():
        # 先立即发一条 start，前端据此立刻创建空的助手气泡
        yield "data: {\"type\":\"start\"}\n\n"

        async with anyio.create_task_group() as tg:
            send_chan, recv_chan = anyio.create_memory_object_stream(32)

            async def _agent():
                async for ev in agent_stream(messages, context=ctx):
                    await send_chan.send(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
                await send_chan.aclose()

            async def _hb():
                async for beat in heartbeat(10.0):
                    await send_chan.send(beat)

            tg.start_soon(_agent)
            tg.start_soon(_hb)

            async with recv_chan:
                async for chunk in recv_chan:
                    # 关键：小块直出，不聚合，防止缓冲
                    yield chunk

    return StreamingResponse(
        merged(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 若前面有 Nginx 反代
        },
    )


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
    #yield {"type": "start"}

    prompt = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    
    # 1️⃣ 默认从 context 取
    station = (context or {}).get("station") if isinstance(context, dict) else None

    # 2️⃣ 如果 context 里没有，或者可能是旧的，就尝试从对话历史里找“已选中基站”
    #    注意：这里会覆盖掉 context.station，确保拿到最新的一次
    for m in reversed(messages):
        if m.get("role") == "assistant" and "已选中基站" in m.get("content", ""):
            # 从 "已选中基站【xxx】（BJS-006）" 里提取 ID
            m2 = re.search(r"（([A-Z]{2,5}-\d{3,6})）", m["content"])
            if m2:
                sid = m2.group(1)
                s = db_json.get_station(sid)
                if s:
                    station = s
                    yield {"type": "log", "channel": "router", "message": f"对话历史确认最新选中：{s.get('name')}（{sid}）"}
            break

    # 3️⃣ 如果还是没有，就走解析逻辑
    if not station:
        s2 = resolve_station_from_prompt(prompt or "")
        if s2:
            station = s2
            yield {"type":"log","channel":"router","message":f"由内容解析到站点：{station.get('name','')}（{station.get('id','')}）"}

    # 然后走 try_direct_answer（问“它的id/坐标/状态/详情”等都会直答，不进模型）

    
    cs = extract_city_status_count(prompt)
    if cs:
        city, status = cs
        rows = db_json.search_stations(city=city, status=status, limit=1000)
        report = render_city_status_report(city, status, rows)
        yield {"type": "log", "channel": "router", "message": f"命中计数直答：{city} / {status} = {len(rows)}（报告体裁）"}
        for line in (report.splitlines(True) or [report]):
            yield {"type": "token", "delta": line}
        yield {"type": "end"}
        return
        
# ✅ 3D 意图优先匹配（放在城市清单直答之前）
    if re.search(r"(3d|三维|立体|体渲染|体积|等值面|等高|模拟)", prompt or "", re.I):
        city3d = extract_city(prompt or "") or "北京"
        rows3d = db_json.search_stations(city=city3d, limit=1000)
        title, spec = chart_specs.spec_3d_city_density_surface(rows3d, city3d)
        yield {"type": "tool", "tool": "plotly", "title": title, "spec": spec}
        yield {"type": "end"}; return

    


# === 可视化意图：用户说“出图/柱状图/图表/plot/bar/chart”等，直接返回 Plotly 规范 ===
    if chart_specs.VIS_HINT_RE.search(prompt or ""):
        city4plot = extract_city(prompt or "") or "北京"
        rows = db_json.search_stations(city=city4plot, limit=1000)
        stats = _aggregate_stats(rows)

        # ① “全部/所有图” → 批量发图 + 总览解读
        if re.search(r"(全部|所有|all|全图)", prompt or "", re.I):
            items = chart_specs.make_all_specs(rows, city4plot)  # [{title, spec}, ...]
            # 先把图批量发给前端（前端要支持 tool=plotly_batch，见下面前端改动）
            yield {"type": "tool", "tool": "plotly_batch", "items": items, "title": f"{city4plot} 图表总览"}

            # 再让模型写一段概览性解读（不必复述每个数字，讲“能看什么”）
            facts_json = json.dumps({
                "city": city4plot,
                "n": stats["n"],
                "vendors": stats["vendor_counts"],
                "status": stats["status_counts"],
                "bands": stats["band_counts"],
            }, ensure_ascii=False)
            explain_prompt = (
                f"你是网络运营分析助手。请用中文给一组图表做**简短总览解读**，对象是{city4plot}的基站数据。\n"
                f"数据事实(JSON)：{facts_json}\n"
                "图表清单：厂商柱状图、在线状态饼图、频段甜甜圈、厂商×状态堆叠柱、厂商×频段热力图、状态水平条、更新时间直方图。\n"
                "写 5-7 句：\n"
                "1) 每个图大概能看什么，不要逐个罗列全部数字。\n"
                "2) 指出最主要的对比/占比/异常（如某厂商占比最高，某状态占比低等）。\n"
                "3) 风格简洁，避免形容词堆叠。"
            )
            async for delta in stream_from_ollama(explain_prompt):
                yield {"type": "token", "delta": delta}
            yield {"type":"end"}; return

        # ② 单图 → 发图 + 针对该图的解读
        title, spec = chart_specs.pick_spec(prompt or "", rows, city4plot)
        kind = _classify_kind(prompt or "")

        # 先发图
        yield {"type":"tool","tool":"plotly","title": title, "spec": spec}

        # 再生成该图的解读（把相关数字塞给模型）
        # 针对不同 kind 准备聚合事实
        focus = {}
        if kind in ("bar", "stacked"):
            focus["vendor_counts"] = stats["vendor_counts"]
            focus["status_counts"] = stats["status_counts"]
        if kind in ("pie", "horizontal"):
            focus["status_counts"] = stats["status_counts"]
        if kind in ("donut",):
            focus["band_counts"] = stats["band_counts"]
        if kind in ("heatmap",):
            # 为简洁起见，给出二维矩阵的“非零格数”和行/列关键统计
            from collections import defaultdict
            vendors = sorted(stats["vendor_counts"].keys())
            bands = sorted(stats["band_counts"].keys())
            mat = defaultdict(dict)
            for v in vendors:
                for b in bands:
                    mat[v][b] = sum(1 for r in rows if (r.get("vendor") or "未知")==v and (r.get("band") or "未知")==b)
            focus["vendor_band_nonzero"] = sum(1 for v in vendors for b in bands if mat[v][b] > 0)
            focus["vendors"] = vendors
            focus["bands"] = bands
        if kind in ("hist",):
            focus["updated_at_summary"] = stats["updated_at_summary"]

        facts_json = json.dumps({
            "city": city4plot,
            "kind": kind,
            "n": stats["n"],
            **focus
        }, ensure_ascii=False)

        explain_prompt = (
            f"你是网络运营分析助手。现在用户让你生成“{title}”。\n"
            f"请用中文写 3-5 句，说明：这个图是什么、它展示了什么维度、读图时应关注哪些对比或占比、并给出 1-2 条简要洞见。\n"
            f"不要复述全部数字，只点出核心结论。避免无意义的套话。城市：{city4plot}。\n"
            f"补充数据(JSON)：{facts_json}"
        )
        async for delta in stream_from_ollama(explain_prompt):
            yield {"type": "token", "delta": delta}
        yield {"type":"end"}; return


    
    # ★ 1.5) 城市清单直答（例如“北京有哪些基站/北京的基站”）
    city = extract_city(prompt or "")
    if want_list(prompt) and city:
        rows = db_json.search_stations(city=city, limit=300)
        report = render_city_overview_report(city, rows)
        yield {"type": "log", "channel": "router", "message": f"命中城市清单直答：{city}（{len(rows)}条，报告体裁）"}
        for line in (report.splitlines(True) or [report]):
            yield {"type": "token", "delta": line}
        yield {"type": "end"}
        return


    direct = try_direct_answer(prompt, station)
    if direct:
        for line in (direct.splitlines(True) or [direct]):
            yield {"type":"token","delta":line}
        yield {"type":"end"}; return
    # （其余城市清单/计数直答、TopK+模型保持不变）


    
    # ★ 2) 上下文护栏
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
        "1) 若用户已选中基站，则优先回答该基站的具体信息；\n"
        "2) 若用户问到某个城市的所有基站，则列出该城市的基站清单（可以用 Markdown 表格展示）；\n"
        "3) 若资料有冲突，以当前选中基站的信息为准。\n"
    )

    aug_prompt = (
        (station_ctx + "\n" if station_ctx else "") +
        (guardrail if station_ctx else "") +
        f"\n用户问题：{prompt}"
    )


   
# ★ 3) 模型兜底：仅给 Top-K 精简表做检索增强，避免全量 JSON
    topk = topk_context_for_prompt(prompt, k=12)
    ctx_md = rows_to_compact_md(topk)
    if ctx_md:
        aug_prompt = ("【可用基站候选（仅供参考）】\n" + ctx_md + "\n\n" + aug_prompt)
        yield {"type": "log", "channel": "router", "message": f"提供 TopK={len(topk)} 行上下文给模型"}

    # === 真流式：直连 Ollama ===
    buf = []
    async for delta in stream_from_ollama(aug_prompt):
        buf.append(delta)
        yield {"type": "token", "delta": delta}

    # （可选）在此对 ''.join(buf) 做 split_think_and_final/后验校验
    yield {"type": "end"}




@app.post("/api/chat/stream")
async def chat_stream(payload: Dict[str, Any] = Body(...)):
    messages = payload.get("messages") or []
    ctx = payload.get("context") or None
    return StreamingResponse(
        sse(agent_stream(messages, context=ctx)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chat")
def chat_once(payload: Dict[str, Any] = Body(...)):
    messages = payload.get("messages") or []
    prompt = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    try:
        text = agent(prompt)
        return {"ok": True, "text": str(text)}
    except Exception as e:
        return {"ok": False, "error": str(e)}