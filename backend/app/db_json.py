
# app/db_json.py
from __future__ import annotations
import os, json, tempfile, threading
from typing import Iterable, List, Dict, Optional
from time import time

# 环境变量可改存储路径；默认 stations.json
STORE_PATH = os.environ.get("STATIONS_JSON", "stations.json")

_LOCK = threading.RLock()
_STATE = {
    "stations": [],   # list[dict]
    "_index": {},     # id -> dict
}

def _atomic_write(path: str, data: dict):
    """原子写入，避免进程崩溃导致文件损坏。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_stations_", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

def _rebuild_index():
    _STATE["_index"] = {s["id"]: s for s in _STATE["stations"]}

def _load_from_disk():
    if not os.path.exists(STORE_PATH):
        _STATE["stations"] = []
        _STATE["_index"] = {}
        return
    with open(STORE_PATH, "r", encoding="utf-8") as f:
        obj = json.load(f)
    # 兼容：允许文件只有 list 或有 {"stations":[...]}
    if isinstance(obj, dict) and "stations" in obj:
        _STATE["stations"] = obj["stations"]
    elif isinstance(obj, list):
        _STATE["stations"] = obj
    else:
        _STATE["stations"] = []
    _rebuild_index()

def _save_to_disk():
    _atomic_write(STORE_PATH, {"stations": _STATE["stations"]})

# ---------- 对外 API ----------

def init_if_missing(seed_stations: Iterable[Dict]):
    """
    首次启动时把 mock_geo 产生的数据持久化到 JSON。
    若文件已存在，则不覆盖（避免每次随机）。
    """
    with _LOCK:
        if os.path.exists(STORE_PATH):
            _load_from_disk()
            return
        _STATE["stations"] = list(seed_stations)
        _rebuild_index()
        _save_to_disk()

def load_all() -> List[Dict]:
    """读取全部站点（从内存缓存；若未加载则先读盘）。"""
    with _LOCK:
        if not _STATE["_index"]:
            _load_from_disk()
        return list(_STATE["stations"])

def get_station(station_id: str) -> Optional[Dict]:
    with _LOCK:
        if not _STATE["_index"]:
            _load_from_disk()
        s = _STATE["_index"].get(station_id)
        return dict(s) if s else None

def upsert_station(st: Dict):
    """插入或更新单个站点，并持久化。"""
    if "id" not in st:
        raise ValueError("station must contain 'id'")
    with _LOCK:
        if not _STATE["_index"]:
            _load_from_disk()
        exists = _STATE["_index"].get(st["id"])
        if exists:
            # 原地更新（保留未提供字段）
            exists.update(st)
        else:
            _STATE["stations"].append(st)
            _STATE["_index"][st["id"]] = st
        _save_to_disk()

def bulk_upsert(stations: Iterable[Dict]):
    with _LOCK:
        if not _STATE["_index"]:
            _load_from_disk()
        for st in stations:
            if "id" not in st:
                continue
            exists = _STATE["_index"].get(st["id"])
            if exists:
                exists.update(st)
            else:
                _STATE["stations"].append(st)
                _STATE["_index"][st["id"]] = st
        _save_to_disk()

def update_status(station_id: str, status: str, updated_at: Optional[int] = None):
    with _LOCK:
        if not _STATE["_index"]:
            _load_from_disk()
        s = _STATE["_index"].get(station_id)
        if not s:
            return
        s["status"] = status
        s["updated_at"] = int(updated_at or time())
        _save_to_disk()

def replace_all(stations: Iterable[Dict]):
    """
    整体替换（谨慎使用）。用于你明确想重置全量数据的场景。
    """
    with _LOCK:
        _STATE["stations"] = list(stations)
        _rebuild_index()
        _save_to_disk()

def search_stations(
    *,
    city: Optional[str] = None,
    vendor: Optional[str] = None,
    band: Optional[str] = None,
    status: Optional[str] = None,
    id_like: Optional[str] = None,
    name_like: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    order_desc_by_updated: bool = True,
) -> List[Dict]:
    """
    纯内存过滤（零依赖）：适合 demo/中小数据量。
    - city/vendor/band/status 精确匹配
    - id_like/name_like 大小写不敏感模糊匹配
    """
    with _LOCK:
        if not _STATE["_index"]:
            _load_from_disk()
        items = _STATE["stations"]

        def like(val: Optional[str], pat: Optional[str]) -> bool:
            if pat is None:
                return True
            if val is None:
                return False
            return pat.lower() in str(val).lower()

        results = []
        for s in items:
            if city   and s.get("city")   != city:   continue
            if vendor and s.get("vendor") != vendor: continue
            if band   and s.get("band")   != band:   continue
            if status and s.get("status") != status: continue
            if id_like   and not like(s.get("id"), id_like):       continue
            if name_like and not like(s.get("name"), name_like):   continue
            results.append(s)

        results.sort(key=lambda x: (x.get("updated_at") or 0),
                     reverse=order_desc_by_updated)
        return [dict(r) for r in results[offset: offset + limit]]