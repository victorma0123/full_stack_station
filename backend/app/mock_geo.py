# app/mock_geo.py
from __future__ import annotations
import random
import time
from typing import Dict, List, Optional
import asyncio
from math import cos, radians, sqrt

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

# ======== 热点（POI）种子，用于把基站“簇”到热点周边 ========
BASE: List[Dict] = [
    # ===== 北京（基础地标）=====
    {"id":"POI-BJ-0001","name":"奥体中心","aliases":["国家奥体中心","国家奥林匹克体育中心","奥体"],
     "city":"北京","district":"朝阳","lat":39.9914,"lng":116.3975,
     "category":"sports","addr_hint":"朝阳区北辰东路","popularity":95,"radius_m":1200},
    {"id":"POI-BJ-0002","name":"国家体育场","aliases":["鸟巢","体育场"],
     "city":"北京","district":"朝阳","lat":39.9929,"lng":116.3964,
     "category":"stadium","addr_hint":"奥林匹克公园内","popularity":98,"radius_m":1200},
    {"id":"POI-BJ-0003","name":"国家游泳中心","aliases":["水立方"],
     "city":"北京","district":"朝阳","lat":39.9920,"lng":116.3871,
     "category":"stadium","addr_hint":"奥林匹克公园内","popularity":92,"radius_m":1000},
    {"id":"POI-BJ-0004","name":"国贸","aliases":["国贸商圈","国贸中心"],
     "city":"北京","district":"朝阳","lat":39.9097,"lng":116.4590,
     "category":"mall","addr_hint":"东三环中路","popularity":97,"radius_m":1000},

    # ===== 上海（基础地标）=====
    {"id":"POI-SH-0001","name":"人民广场","aliases":["市政府","大剧院"],
     "city":"上海","district":"黄浦","lat":31.2304,"lng":121.4737,
     "category":"square","addr_hint":"黄浦区","popularity":96,"radius_m":800},
    {"id":"POI-SH-0002","name":"东方明珠","aliases":["东方明珠塔"],
     "city":"上海","district":"浦东","lat":31.2397,"lng":121.4998,
     "category":"landmark","addr_hint":"陆家嘴","popularity":99,"radius_m":1500},

    # ===== 广州（基础地标）=====
    {"id":"POI-GZ-0001","name":"北京路步行街","aliases":["北京路"],
     "city":"广州","district":"越秀","lat":23.1251,"lng":113.2708,
     "category":"mall","addr_hint":"越秀区","popularity":93,"radius_m":900},

    # ===== 深圳（基础地标）=====
    {"id":"POI-SZ-0001","name":"深圳北站","aliases":["高铁北站"],
     "city":"深圳","district":"龙华","lat":22.6091,"lng":114.0295,
     "category":"transport","addr_hint":"龙华区民治","popularity":95,"radius_m":1200},

    # ===== 杭州（基础地标）=====
    {"id":"POI-HZ-0001","name":"西湖","aliases":["西湖风景区"],
     "city":"杭州","district":"西湖","lat":30.2431,"lng":120.1500,
     "category":"scenic","addr_hint":"西湖区","popularity":99,"radius_m":1500},

    # ===== 跨城同名：万达广场 =====
    {"id":"POI-BJ-1001","name":"万达广场","aliases":["万达","Wanda Plaza"],
     "city":"北京","district":"朝阳","lat":39.9231,"lng":116.4865,
     "category":"mall","addr_hint":"朝阳区建国路","popularity":90,"radius_m":900},
    {"id":"POI-BJ-1002","name":"万达广场","aliases":["万达","Wanda Plaza"],
     "city":"北京","district":"石景山","lat":39.9135,"lng":116.2238,
     "category":"mall","addr_hint":"石景山区鲁谷","popularity":86,"radius_m":900},
    {"id":"POI-SH-1001","name":"万达广场","aliases":["万达","Wanda Plaza"],
     "city":"上海","district":"闵行","lat":31.1152,"lng":121.3897,
     "category":"mall","addr_hint":"闵行区都会路","popularity":88,"radius_m":900},
    {"id":"POI-GZ-1001","name":"万达广场","aliases":["万达","Wanda Plaza"],
     "city":"广州","district":"番禺","lat":22.9378,"lng":113.3650,
     "category":"mall","addr_hint":"番禺区汉溪大道","popularity":87,"radius_m":900},
    {"id":"POI-SZ-1001","name":"万达广场","aliases":["万达","Wanda Plaza"],
     "city":"深圳","district":"龙岗","lat":22.7206,"lng":114.2467,
     "category":"mall","addr_hint":"龙岗大道","popularity":85,"radius_m":900},
    {"id":"POI-HZ-1001","name":"万达广场","aliases":["万达","Wanda Plaza"],
     "city":"杭州","district":"滨江","lat":30.1881,"lng":120.2099,
     "category":"mall","addr_hint":"滨江区江陵路","popularity":84,"radius_m":900},

    # ===== 同城多点同名：奥体中心（示例补点）=====
    {"id":"POI-BJ-1101","name":"奥体中心","aliases":["奥体"],
     "city":"北京","district":"朝阳","lat":39.9895,"lng":116.4010,
     "category":"sports","addr_hint":"北辰东路（主场馆区）","popularity":88,"radius_m":1000},
    {"id":"POI-BJ-1102","name":"奥体中心","aliases":["奥体"],
     "city":"北京","district":"海淀","lat":39.9810,"lng":116.3610,
     "category":"sports","addr_hint":"北三环西段（训练/拓展区）","popularity":80,"radius_m":1000},

    # ===== 其他重名：来福士广场 =====
    {"id":"POI-BJ-1201","name":"来福士广场","aliases":["来福士","Raffles City"],
     "city":"北京","district":"东城","lat":39.9126,"lng":116.4342,
     "category":"mall","addr_hint":"东直门南大街","popularity":89,"radius_m":900},
    {"id":"POI-SH-1201","name":"来福士广场","aliases":["来福士","Raffles City"],
     "city":"上海","district":"黄浦","lat":31.2318,"lng":121.4750,
     "category":"mall","addr_hint":"人民广场商圈","popularity":91,"radius_m":900},
]

# 不同类别默认“簇半径”上限（米），生成时会在 [0, spread] 内取随机距离
CATEGORY_SPREAD_M = {
    "sports": 700,
    "stadium": 700,
    "mall": 600,
    "square": 500,
    "transport": 800,
    "scenic": 1000,
    "landmark": 900,
}

def _meters_to_deg(lat: float, dx_m: float, dy_m: float) -> tuple[float, float]:
    """把以米为单位的偏移转换成经纬度偏移（近似）。dx: 东西向, dy: 南北向"""
    dlat = dy_m / 111_000.0
    dlon = dx_m / (111_000.0 * max(0.1, cos(radians(lat))))
    return dlat, dlon

def _pick_city_pois(city: str) -> List[Dict]:
    return [p for p in BASE if p.get("city") == city]

def _sample_poi(city_pois: List[Dict]) -> Dict:
    # 按 popularity 加权抽样
    weights = [max(1, int(p.get("popularity") or 50)) for p in city_pois]
    return random.choices(city_pois, weights=weights, k=1)[0]

def _gen_one_near_poi(city: str, code: str, idx: int, poi: Dict) -> Dict:
    band = random.choice(BANDS)
    status = random.choices(STATUS, weights=STATUS_W, k=1)[0]
    vendor = random.choice(VENDORS)

    lat0, lng0 = float(poi["lat"]), float(poi["lng"])
    spread = CATEGORY_SPREAD_M.get(poi.get("category",""), 700)

    # 不同频段给不同“典型距离”系数（让 n28 更远，n78 更近）
    band_scale = {"n78": 0.5, "n41": 0.7, "n1": 0.9, "n28": 1.1}.get(band, 0.8)
    r = random.uniform(100, spread) * band_scale  # 100m 起步，至 spread
    ang = random.uniform(0, 360)
    dx = r * cos(radians(ang))
    dy = r * (1 if random.random()>0.5 else -1) * sqrt(max(0.0, 1 - (cos(radians(ang)))**2))  # 保持均匀

    dlat, dlon = _meters_to_deg(lat0, dx, dy)
    lat = round(lat0 + dlat, 6)
    lng = round(lng0 + dlon, 6)

    # 描述里写清“靠近哪个 POI”，并给出区县/地址提示与大概距离
    desc = f"靠近 {poi.get('name')}（{poi.get('district') or '—'}·{poi.get('addr_hint') or '—'}），直线约 {int(r)} 米。{random.choice(EXTRA_DESC)}"

    return {
        "id": f"{code}-{idx:03d}",
        "city": city,
        "name": f"{city}-示例站{idx}",
        "lat": lat,
        "lng": lng,
        "vendor": vendor,
        "band": band,
        "status": status,
        "updated_at": int(time.time()),
        "desc": desc,
        "poi_id": poi.get("id"),
    }

def _gen_one_near_center(city: str, code: str, idx: int, center: tuple) -> Dict:
    lat0, lng0 = center
    # 相比旧逻辑，把扩散半径缩小到 ~1.2km，尽量别离 POI 太远（仍保留一部分“非 POI 簇”）
    lat = lat0 + random.uniform(-0.010, 0.010)
    lng = lng0 + random.uniform(-0.010, 0.010)

    return {
        "id": f"{code}-{idx:03d}",
        "city": city,
        "name": f"{city}-示例站{idx}",
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "vendor": random.choice(VENDORS),
        "band": random.choice(BANDS),
        "status": random.choices(STATUS, weights=STATUS_W, k=1)[0],
        "updated_at": int(time.time()),
        "desc": random.choice(EXTRA_DESC),
        "poi_id": None,
    }

def _gen_stations(city: str, code: str, center: tuple, n: int = 16) -> List[Dict]:
    """
    生成 n 个站：
    - ~70% 簇在该城市的 POI 周边（按 popularity 加权）
    - ~30% 均匀散在城市中心附近（保持一些“非热点”点位）
    """
    stations: List[Dict] = []
    city_pois = _pick_city_pois(city)
    use_poi_ratio = 0.7 if city_pois else 0.0

    for i in range(1, n + 1):
        use_poi = (random.random() < use_poi_ratio)
        if use_poi:
            poi = _sample_poi(city_pois)
            s = _gen_one_near_poi(city, code, i, poi)
        else:
            s = _gen_one_near_center(city, code, i, center)
        stations.append(s)
    return stations

def seed(seed_value: Optional[int] = None):
    if seed_value is not None:
        random.seed(seed_value)
    for city, code, center in CITY_CFG:
        GEO[city] = {
            "code": code,
            "center": {"lat": center[0], "lng": center[1]},
            "stations": _gen_stations(city, code, center, n=16),
        }

def list_cities() -> List[Dict]:
    return [{"name": c, "code": GEO[c]["code"], "center": GEO[c]["center"]} for c in GEO]

def list_stations(city: str, randomize_status: bool = False) -> List[Dict]:
    if city not in GEO:
        return []
    st = GEO[city]["stations"]
    if randomize_status:
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
        session_id = "__default__"
    async with LOCK:
        SELECTED_BY_SESSION[session_id] = s
    return s

def get_selected(session_id: Optional[str]) -> Optional[Dict]:
    return SELECTED_BY_SESSION.get(session_id or "__default__")

# 初始化数据（模块导入时）
seed()
