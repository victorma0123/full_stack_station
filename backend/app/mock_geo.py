
# app/mock_geo.py
from __future__ import annotations
import random
import time
from typing import Dict, List, Optional
import asyncio

# ===== 内存存储 =====
GEO: Dict[str, Dict] = {}                 # { city_name: { code, center, stations: [] } }
SELECTED_BY_SESSION: Dict[str, Dict] = {} # { session_id: station_dict }
LOCK = asyncio.Lock()

VENDORS = ["Huawei", "ZTE", "Ericsson", "Nokia"]
BANDS   = ["n78", "n41", "n28", "n1"]
STATUS  = ["online", "maintenance", "offline"]
STATUS_W = [0.7,       0.2,            0.1]   # 随机权重

CITY_CFG = [
    #  city,  code,   (lat, lng)
    ("北京", "BJS", (39.9042, 116.4074)),
    ("上海", "SHS", (31.2304, 121.4737)),
    ("广州", "GZS", (23.1291, 113.2644)),
    ("深圳", "SZS", (22.5431, 114.0579)),
    ("杭州", "HZS", (30.2741, 120.1551)),
]
EXTRA_DESC = [
    "该站点周边为居民区，覆盖半径约 500 米，晚高峰时段负载较高。",
    "位于写字楼密集区域，用户投诉主要集中在室内覆盖不足。",
    "周边有大型商场，周末人流量大，容易出现拥塞。",
    "位于地铁口附近，早晚高峰有干扰告警记录。",
    "周边为公园绿地，覆盖稳定，但偶尔有掉话情况。",
]

def _gen_stations(city: str, code: str, center: tuple, n: int = 8) -> List[Dict]:
    lat0, lng0 = center
    stations = []
    for i in range(1, n + 1):
        # 在城市中心附近随机一个小偏移（~0.01 度 ≈ 1km）
        lat = lat0 + random.uniform(-0.03, 0.03)
        lng = lng0 + random.uniform(-0.03, 0.03)
        s = {
            "id": f"{code}-{i:03d}",
            "city": city,
            "name": f"{city}-示例站{i}",
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "vendor": random.choice(VENDORS),
            "band": random.choice(BANDS),
            "status": random.choices(STATUS, weights=STATUS_W, k=1)[0],
            "updated_at": int(time.time()),
            "desc": random.choice(EXTRA_DESC),# ✅ 新增非结构化描述
        }
        stations.append(s)
    return stations

def seed(seed_value: Optional[int] = None):
    if seed_value is not None:
        random.seed(seed_value)
    for city, code, center in CITY_CFG:
        GEO[city] = {
            "code": code,
            "center": {"lat": center[0], "lng": center[1]},
            "stations": _gen_stations(city, code, center, n=8),
        }

def list_cities() -> List[Dict]:
    return [{"name": c, "code": GEO[c]["code"], "center": GEO[c]["center"]} for c in GEO]

def list_stations(city: str, randomize_status: bool = False) -> List[Dict]:
    if city not in GEO:
        return []
    st = GEO[city]["stations"]
    if randomize_status:
        # 每次拉取时随机刷新状态
        for s in st:
            s["status"] = random.choices(STATUS, weights=STATUS_W, k=1)[0]
            s["updated_at"] = int(time.time())
    return st

def get_station(station_id: str) -> Optional[Dict]:
    for c in GEO.values():
        for s in c["stations"]:
            if s["id"] == station_id:
                return s
    return None

async def record_selection(session_id: Optional[str], station_id: str) -> Optional[Dict]:
    """记录“某会话选中了哪个基站”，并返回该站详情。"""
    s = get_station(station_id)
    if not s:
        return None
    if not session_id:
        # 允许无 session_id；存一个特殊键
        session_id = "__default__"
    async with LOCK:
        SELECTED_BY_SESSION[session_id] = s
    return s

def get_selected(session_id: Optional[str]) -> Optional[Dict]:
    return SELECTED_BY_SESSION.get(session_id or "__default__")

# 初始化数据（模块导入时）
seed()