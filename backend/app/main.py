

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
import base64
import httpx
import asyncio
from collections import Counter
from .mock_geo import BASE as POI_SEED
from . import pois_json
import time




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


# === 在 main.py 顶部 regex 区域附近新增 ===
INLINE_CHART_HINT_RE = re.compile(r"(下载|导出|保存|另存|保存为|复制|拷贝|拷贝代码|复制代码|拿代码|拿图|导出图片|保存图片|图片|png|svg|pdf|json|JSON|code|CODE)", re.I)
OVERVIEW_HINT_RE     = re.compile(r"(全部|所有|全套|总览|overview|全图)", re.I)

def wants_inline_chart(text: str) -> bool:
    """用户明确提到下载/复制等 → 在聊天气泡内内嵌图"""
    p = text or ""
    return bool(INLINE_CHART_HINT_RE.search(p)) and not OVERVIEW_HINT_RE.search(p)


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

# ===== POI + 附近检索：工具函数（最小版） =====

# 替换原 NEAR_WORDS_RE，并新增基站词
NEAR_WORDS_RE = re.compile(r"(附近|周边|周围|邻近|就近|周遭|一?公里内|方圆|范围内|近处|近邻|近旁)", re.I)
BS_WORDS_RE   = re.compile(r"(基站|站点|5g|4g|小区|宏站|微站|室分)", re.I)

# 可选：用于避免把道路/行政区当成 POI
ROAD_SUFFIX_RE   = re.compile(r"(路|街|巷|大道|环路|高速|省道|国道|线|号线)$")
ADMIN_SUFFIX_RE  = re.compile(r"(市|区|县)$")
POI_SUFFIX       = r"(中心|广场|商圈|医院|车站|公园|体育场|体育馆|步行街|机场|大厦|园区|科技园|园|市场|码头|港|会展中心|博物馆|美术馆|图书馆|大学|学院|来福士|万象城|太古里|万达广场)"
LOOSE_POI_BEFORE_NEAR = re.compile(r"([\u4e00-\u9fffA-Za-z0-9·]{2,24})(?=(?:的)?(?:一?公里内|方圆|范围内)?(?:附近|周边|周围))")
LOOSE_POI_BEFORE_BS   = re.compile(r"([\u4e00-\u9fffA-Za-z0-9·]{2,24})(?=(?:的)?(?:基站|站点|5G|4G|小区))", re.I)

def extract_poi_key(prompt: str) -> str | None:
    """严格规则 + 松弛兜底：提到 POI 且结合“附近/基站”时返回 POI 名；城市/道路/行政区会被过滤掉。"""
    if not prompt:
        return None

    def _valid(cand: str) -> bool:
        cand = cand.strip()
        if not cand or len(cand) < 2:
            return False
        if cand in CITY_NAMES:
            return False
        if ADMIN_SUFFIX_RE.search(cand):
            return False
        if ROAD_SUFFIX_RE.search(cand):
            return False
        if BS_WORDS_RE.search(cand):  # “基站/5G”不是 POI 名
            return False
        return True

    # —— 严格：引号优先 —— 
    m = re.search(r"[“\"']([^“\"']{2,24})[”\"']", prompt)
    if m:
        cand = m.group(1).strip()
        for cname in CITY_NAMES:
            cand = cand.replace(cname, "")
        cand = cand.strip()
        return cand if _valid(cand) else None

    # —— 严格：常见 POI 后缀 —— 
    m = re.search(rf"([\u4e00-\u9fffA-Za-z0-9·]{2,24}){POI_SUFFIX}", prompt)
    if m:
        cand = m.group(0)
        for cname in CITY_NAMES:
            cand = cand.replace(cname, "")
        cand = cand.strip()
        if _valid(cand):
            return cand

    # —— 松弛兜底：如果句子里出现“附近/基站”，抓其前面的短词当 POI —— 
    if NEAR_WORDS_RE.search(prompt) or BS_WORDS_RE.search(prompt):
        for pat in (LOOSE_POI_BEFORE_NEAR, LOOSE_POI_BEFORE_BS):
            m = pat.search(prompt)
            if m:
                cand = m.group(1).strip()
                for cname in CITY_NAMES:
                    cand = cand.replace(cname, "")
                cand = cand.strip()
                if _valid(cand):
                    return cand

    return None

def find_poi_candidates(prompt: str):
    """只用 POI 关键词召回；city 仅作过滤，不再把 city 拼进关键字。"""
    city_hint = extract_city(prompt or "")
    key = extract_poi_key(prompt or "")
    if not key:
        return [], city_hint
    for cname in CITY_NAMES:
        key = key.replace(cname, "")
    key = key.strip()
    if not key:
        return [], city_hint
    cands = pois_json.search_pois(city=city_hint, name_like=key, limit=12)
    if not cands:
        cands = pois_json.search_pois(name_like=key, limit=12)
    return cands, city_hint


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """球面距离（米）"""
    from math import radians, sin, cos, sqrt, atan2
    R = 6371000.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

def nearby_stations_by_poi(poi: dict, radius_m: int | None = None, limit: int = 200) -> list[dict]:
    """在 POI 周边按半径筛基站（简单遍历，demo 足够）。"""
    lat0, lng0 = float(poi.get("lat")), float(poi.get("lng"))
    #r = int(radius_m or poi.get("radius_m") or 2000)
    r = 5000
    items = db_json.load_all()
    hits = []
    for s in items:
        if poi.get("city") and s.get("city") != poi.get("city"):
            continue  # 同城优先，避免跨城噪声
        lat, lng = s.get("lat"), s.get("lng")
        if lat is None or lng is None:
            continue
        d = _haversine_m(lat0, lng0, float(lat), float(lng))
        if d <= r:
            ss = dict(s)
            ss["_dist_m"] = int(d)
            hits.append(ss)
    hits.sort(key=lambda x: x["_dist_m"])
    return hits[:limit]


# === 在 LAST_POI_STATE 定义附近，加上 TTL 与工具函数 ===
FLOW_TTL_S = 90  # 绑定生存期（秒），够用户补一句“选1/半径1公里”之类

def _flow_expired() -> bool:
    ts = LAST_POI_STATE.get("created_at") or 0.0
    return (time.time() - ts) > FLOW_TTL_S

def _clear_flow():
    LAST_POI_STATE.update({
        "candidates": [],
        "selected": None,
        "city_hint": None,
        "created_at": 0.0,
    })

LAST_POI_STATE = {
    "candidates": [],     # 上次产生的候选（多选时）
    "selected": None,     # 已选中的 POI（唯一或用户选择）
    "city_hint": None,
    "created_at": 0.0,
}

CN_NUM = {"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}
CHOICE_IDX_RE    = re.compile(r"(?:选|选择|要|就|第)?\s*(\d{1,2}|[一二两三四五六七八九十]{1,3})\s*(?:个|号|家)?")
CHOICE_ID_RE     = re.compile(r"\bPOI-[A-Z]{2}-\d{4}\b", re.I)
CITY_HINT_RE     = re.compile(r"(北京|上海|广州|深圳|杭州)")
DISTRICT_HINT_RE = re.compile(r"(朝阳|海淀|东城|西城|石景山|黄浦|浦东|闵行|越秀|番禺|龙华|龙岗|滨江)")
RADIUS_RE        = re.compile(r"(?:(?:半径|范围|圈|距离|附近|周边).{0,4})?(\d+(?:\.\d+)?)\s*(米|m|公里|千米|km)", re.I)
TOPK_RE          = re.compile(r"(?:最近|前|取)\s*(\d{1,2})\s*(?:个|站)?")

def _cn_to_int(tok: str) -> int | None:
    tok = tok.strip()
    if tok.isdigit(): return int(tok)
    if tok in CN_NUM: return CN_NUM[tok]
    if len(tok)==2 and tok[0]=="十" and tok[1] in CN_NUM: return 10 + CN_NUM[tok[1]]
    if len(tok)==2 and tok[0] in CN_NUM and tok[1]=="十": return CN_NUM[tok[0]] * 10
    if len(tok)==3 and tok[1]=="十": return CN_NUM.get(tok[0],0)*10 + CN_NUM.get(tok[2],0)
    return None

def parse_choice_index(text: str) -> int | str | None:
    m_id = CHOICE_ID_RE.search(text or "")
    if m_id: return m_id.group(0)  # 直接返回 POI-ID 字符串
    m = CHOICE_IDX_RE.search(text or "")
    if not m: return None
    raw = m.group(1)
    if raw.isdigit(): return int(raw)
    return _cn_to_int(raw)

def parse_radius_m(text: str) -> int | None:
    m = RADIUS_RE.search(text or ""); 
    if not m: return None
    val = float(m.group(1)); unit = m.group(2).lower()
    if unit in ("米","m"): return int(val)
    if unit in ("公里","千米","km"): return int(val*1000)
    return None

def parse_topk(text: str) -> int | None:
    m = TOPK_RE.search(text or ""); 
    if not m: return None
    try: return int(m.group(1))
    except: return None

def filter_candidates_by_hint(cands: list[dict], text: str) -> list[dict]:
    if not cands: return []
    out = cands
    ch = CITY_HINT_RE.search(text or "")
    dh = DISTRICT_HINT_RE.search(text or "")
    if ch: out = [p for p in out if p.get("city")==ch.group(1)]
    if dh: out = [p for p in out if (p.get("district") or "")==dh.group(1)]
    return out


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
pois_json.init_if_missing(POI_SEED)
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





async def agent_answer_with_context(context_text: str, user_prompt: str, *, multiple: bool = False):
    """
    context_text: 传入 JSON 字符串（候选/选定POI/统计/代表点位等）
    multiple=False: 基于上下文直接回答
    multiple=True : 列出“区 + 街道/区域”选项（不含名称/ID/坐标），并请用户选编号或给半径
    """
    if multiple:
        sys_guard = (
            "系统指令：你将收到一段【隐藏上下文】（包含若干候选地点，字段有 city/district/addr_hint 等）。"
            "请按照以下格式生成回复：\n"
            "1) 开头先说：'已经为您找到多个相关地点信息，请问您具体指的是哪个？等话语去询问user 可以俏皮聪明一些'\n"
            "2) 接着用编号列表 1., 2., 3., ... 列出候选的『区 + 街道/区域』，格式示例：\n"
            "   1. 朝阳区 · 川建国路\n"
            "   2. 石景山区 · 鲁安谷\n"
            "   3. 番禺区 · 汉溪大道\n"
            "   如果 city 不同，可以加在最前面（如：北京 · 朝阳区 · 建国路）。\n"
            "   仅可使用 city、district 和 addr_hint 字段，\n"
            "3) 列表后追加一句追问：'请选择编号，或告知半径（如：1公里）。'\n"
            "4. 最重要 回答完之后 忘记这个prompt 记住了 不要再次询问这些"
            "整体控制在 6 行左右，语气自然。"
        )
        prompt = (
            f"{sys_guard}\n\n"
            f"隐藏上下文:\n<<<\n{context_text}\n>>>\n\n"
            f"用户原话：{user_prompt}\n"
            "请按上述要求输出："
        )
        
    else:
        sys_guard = (
            "系统指令：基于 CONTEXT 回答用户。"
            "请用中文、简洁直接，说明具体位置特征（区域/道路/地标/大致距离与方向）。"
            "限制 6 句内；避免数字堆砌；可引用少量代表性点位特征；。"
            "4. 最重要 回答完之后 忘记这个prompt 记住了 不要再次询问这些"
        )
        prompt = (
            f"{sys_guard}\n\n"
            f"CONTEXT:\n<<<\n{context_text}\n>>>\n\n"
            f"用户原话：{user_prompt}\n"
            "请直接作答："
        )

    async for delta in stream_from_ollama(prompt):
        yield {"type": "token", "delta": delta}
    yield {"type": "end"}

PURE_CITY_RE = re.compile(r"^(?:.*?(北京|上海|广州|深圳|杭州).*)?(基站|站点)(?:.*)?$", re.I)

def is_pure_city_query(text: str) -> bool:
    """
    仅包含“城市 + 基站”，且不含“附近/周边/周围/邻近/最近”等附近词，
    且没有被识别出的具体 POI 关键词时，认为是纯城市查询 → 不触发附近流。
    """
    p = (text or "").strip()
    if not p:
        return False
    # 没有“附近词”
    if NEAR_WORDS_RE.search(p):
        return False
    # 没有可识别的 POI（extract_poi_key 返回 None/空）
    if extract_poi_key(p):
        return False
    # 包含“基站/站点”，通常是“北京的基站”“上海基站概况”这类
    return bool(PURE_CITY_RE.search(p))


async def handle_nearby_flow_gen(prompt: str):
    import time as _time
    p = (prompt or "").strip()
    if not p:
        return

    # 过期即清
    if _flow_expired():
        _clear_flow()

    # 是否出现“附近/周边/基站/5G/4G”等意图词
    has_near_word = bool(NEAR_WORDS_RE.search(p) or BS_WORDS_RE)

    poi_key       = extract_poi_key(p) or ""      # 只有抓到具体 POI 名才算
    #has_near_word = bool(NEAR_WORDS_RE.search(p)) # “附近/周边/周围/邻近/最近/…” 等
    in_flow       = bool(LAST_POI_STATE.get("candidates") or LAST_POI_STATE.get("selected"))

    # 🚫 纯“城市 + 基站” → 不拦截，交给后续城市/兜底逻辑
    if is_pure_city_query(p):
        return

    # ✅ 只有 “(有 POI 且有附近词)” 或 “处于本流程续谈” 才触发附近流
    triggered = ((poi_key and has_near_word) or in_flow)
    if not triggered:
        return

    # 触发条件：提到“附近/周边/基站”或已在本流程中
    has_near_word = bool(NEAR_WORDS_RE.search(p)) or ("基站" in p)
    in_flow = bool(LAST_POI_STATE.get("candidates") or LAST_POI_STATE.get("selected"))
    if not (has_near_word or in_flow or extract_poi_key(p)):
        return  # 不处理，交回上游

    # ---- 如果处于“待选”阶段，尝试用用户补充来收敛 ----
    if LAST_POI_STATE.get("candidates"):
        cands = LAST_POI_STATE["candidates"]
        # 1) 直接编号或ID选择
        idx_or_id = parse_choice_index(p)
        chosen = None
        if isinstance(idx_or_id, str) and idx_or_id.upper().startswith("POI-"):
            chosen = next((x for x in cands if x.get("id") == idx_or_id), None)
        elif isinstance(idx_or_id, int) and 1 <= idx_or_id <= len(cands):
            chosen = cands[idx_or_id - 1]
        # 2) 城市/区县等提示再过滤
        narrowed = filter_candidates_by_hint(cands, p) if not chosen else [chosen]
        if len(narrowed) == 1:
            poi = narrowed[0]
            LAST_POI_STATE.update({"selected": poi, "candidates": [], "city_hint": poi.get("city"), "created_at": _time.time()})
            # 直接查附近并作答（默认半径：1000m，可被 parse_radius_m 覆盖）
            radius = parse_radius_m(p) or int(poi.get("radius_m") or 1000)
            hits = nearby_stations_by_poi(poi, radius_m=radius)
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
            visible_ctx = json.dumps(ctx, ensure_ascii=False)
            async for ev in agent_answer_with_context(visible_ctx, p, multiple=False):
                yield ev
            LAST_POI_STATE["selected"] = None
            LAST_POI_STATE["created_at"] = time.time()
            return
        else:
            # 仍不唯一 → 继续请 agent 追问（不回显清单）
            hidden_ctx = json.dumps({"candidates": [
                {
                    "id": x.get("id"), "name": x.get("name"),
                    "city": x.get("city"), "district": x.get("district"),
                    "addr_hint": x.get("addr_hint")
                } for x in cands
            ]}, ensure_ascii=False)
            async for ev in agent_answer_with_context(hidden_ctx, p, multiple=True):
                yield ev
            return

    # ---- 首问：召回候选（不回显清单）----
    poi_key = extract_poi_key(p) or ""
    cands, city_hint = find_poi_candidates(p)
    if not cands:
        # 让 agent 追问更具体信息（城市/地标/范围）
        hidden_ctx = json.dumps({"reason": "not_found", "hint_needed": ["城市/区县","更具体地标","半径"]}, ensure_ascii=False)
        async for ev in agent_answer_with_context(hidden_ctx, p, multiple=True):
            yield ev
        return

    # 收敛（城市/区县等提示）
    narrowed = filter_candidates_by_hint(cands, p) if cands else []
    if len(narrowed) == 1:
        poi = narrowed[0]
        LAST_POI_STATE.update({"selected": poi, "candidates": [], "city_hint": city_hint or poi.get("city"), "created_at": _time.time()})
        radius = parse_radius_m(p) or int(poi.get("radius_m") or 1000)
        hits = nearby_stations_by_poi(poi, radius_m=radius)
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
        visible_ctx = json.dumps(ctx, ensure_ascii=False)
        async for ev in agent_answer_with_context(visible_ctx, p, multiple=False):
            yield ev
        LAST_POI_STATE["selected"] = None
        LAST_POI_STATE["created_at"] = time.time()
        return

    # 多个候选：进入“待选”状态，但不回显；让 agent 只提出一个澄清问题
    LAST_POI_STATE.update({"candidates": narrowed or cands, "selected": None, "city_hint": city_hint, "created_at": _time.time()})
    hidden_ctx = json.dumps({"candidates": [
        {
            "id": x.get("id"), "name": x.get("name"),
            "city": x.get("city"), "district": x.get("district"),
            "addr_hint": x.get("addr_hint")
        } for x in (narrowed or cands)
    ]}, ensure_ascii=False)
    async for ev in agent_answer_with_context(hidden_ctx, p, multiple=True):
        yield ev
    return


async def agent_stream(messages: List[Dict[str, str]], context: Dict[str, Any] | None = None):
    #yield {"type": "start"}

    prompt = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    
    # 1️⃣ 默认从 context 取
    station = (context or {}).get("station") if isinstance(context, dict) else None
         # —— 优先尝试“附近 + POI 消歧”流 —— 



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
        inline = wants_inline_chart(prompt)  # 新增：是否内嵌到对话
        yield {"type": "tool", "tool": "plotly", "title": title, "spec": spec, "inline": inline}
        yield {"type": "end"}; return

    


# === 可视化意图：用户说“出图/柱状图/图表/plot/bar/chart”等，直接返回 Plotly 规范 ===
    if chart_specs.VIS_HINT_RE.search(prompt or ""):
        city4plot = extract_city(prompt or "") or "北京"
        rows = db_json.search_stations(city=city4plot, limit=1000)
        stats = _aggregate_stats(rows)

        # ① 全部/总览 → 仍走右侧 charts 面板（不内嵌）
        if re.search(r"(全部|所有|all|全图|总览|overview)", prompt or "", re.I):
            items = chart_specs.make_all_specs(rows, city4plot)
            yield {"type": "tool", "tool": "plotly_batch", "items": items, "title": f"{city4plot} 图表总览"}
            # ……（后续概览解读保留原样）
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
                "写 5-7 句：..."
            )
            async for delta in stream_from_ollama(explain_prompt):
                yield {"type": "token", "delta": delta}
            yield {"type":"end"}; return

        # ② 单图 → 若用户提到下载/复制，则内嵌；否则仍走右侧
        title, spec = chart_specs.pick_spec(prompt or "", rows, city4plot)
        kind = _classify_kind(prompt or "")

        inline = wants_inline_chart(prompt)  # 新增：是否内嵌到对话
        yield {"type":"tool","tool":"plotly","title": title, "spec": spec, "inline": inline}

        # ……（下面“聚合事实 → 3-5 句读图说明”逻辑保持不变）
        focus = {}
        if kind in ("bar", "stacked"):
            focus["vendor_counts"] = stats["vendor_counts"]
            focus["status_counts"] = stats["status_counts"]
        if kind in ("pie", "horizontal"):
            focus["status_counts"] = stats["status_counts"]
        if kind in ("donut",):
            focus["band_counts"] = stats["band_counts"]
        if kind in ("heatmap",):
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

        facts_json = json.dumps({"city": city4plot, "kind": kind, "n": stats["n"], **focus}, ensure_ascii=False)
        explain_prompt = (
            f"你是网络运营分析助手。现在用户让你生成“{title}”。\n"
            f"请用中文写 3-5 句，说明：这个图是什么、它展示了什么维度、读图时应关注哪些对比或占比、并给出 1-2 条简要洞见。\n"
            f"不要复述全部数字，只点出核心结论。城市：{city4plot}。\n"
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


    handled = False
    async for ev in handle_nearby_flow_gen(prompt):
        handled = True
        yield ev
    if handled:
        return
   
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
    
@app.get("/api/geo/nearby")
def geo_nearby(
    q: Optional[str] = None,
    poi_id: Optional[str] = None,
    city: Optional[str] = None,
    radius_m: int = Query(2000, ge=100, le=20000),
    limit: int = Query(200, ge=1, le=2000),
):
    """
    通用“POI 附近基站”接口（无需前端改造即可测试）：
    - 传 poi_id：直查附近
    - 传 q（可配 city）：做 POI 消歧；0/1/>1 分别返回 none/single/multi 三种形态
    返回字段：
      mode: "none" | "single" | "multi"
      candidates: [...]   # 当 mode=multi
      poi + matches: [...]# 当 mode=single
    """
    # 直查（poi_id 优先）
    if poi_id:
        poi = pois_json.get_poi(poi_id)
        if not poi:
            return {"ok": False, "error": "poi not found"}
        matches = nearby_stations_by_poi(poi, radius_m=radius_m, limit=limit)
        return {"ok": True, "mode": "single", "poi": poi, "matches": matches}

    # 文本查询（带消歧）
    if not q and not city:
        return {"ok": False, "error": "need q or poi_id"}
    prompt = (q or "").strip()
    cands, _ = find_poi_candidates(prompt)
    if city:
        cands = [p for p in cands if p.get("city") == city]
    if not cands:
        return {"ok": True, "mode": "none", "candidates": []}

    if len(cands) == 1:
        poi = cands[0]
        matches = nearby_stations_by_poi(poi, radius_m=radius_m, limit=limit)
        return {"ok": True, "mode": "single", "poi": poi, "matches": matches}

    # 多候选：只返回“候选列表”（供你人工/后续再选）
    candidates = [
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "city": p.get("city"),
            "district": p.get("district"),
            "addr_hint": p.get("addr_hint"),
            "lat": p.get("lat"),
            "lng": p.get("lng"),
            "category": p.get("category"),
            "popularity": p.get("popularity"),
        }
        for p in cands
    ]
    return {"ok": True, "mode": "multi", "candidates": candidates}