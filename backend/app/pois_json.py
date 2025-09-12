



# app/pois_json.py
from __future__ import annotations
import os, json, tempfile, threading
from typing import List, Dict, Optional
from time import time

STORE_PATH = os.environ.get("POIS_JSON", "pois.json")

_LOCK = threading.RLock()
_STATE = {"pois": [], "_index": {}}  # _index: id -> poi

# —— 内部工具 ——

def _atomic_write(path: str, data: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_pois_", dir=os.path.dirname(path) or ".")
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
    _STATE["_index"] = {p["id"]: p for p in _STATE["pois"]}

def _load_from_disk():
    if not os.path.exists(STORE_PATH):
        _STATE["pois"] = []
        _STATE["_index"] = {}
        return
    with open(STORE_PATH, "r", encoding="utf-8") as f:
        obj = json.load(f)
    if isinstance(obj, dict) and "pois" in obj:
        _STATE["pois"] = obj["pois"]
    elif isinstance(obj, list):
        _STATE["pois"] = obj
    else:
        _STATE["pois"] = []
    _rebuild_index()

def _save_to_disk():
    _atomic_write(STORE_PATH, {"pois": _STATE["pois"]})

# —— 对外 API ——

def init_if_missing(seed_pois: List[Dict]):
    with _LOCK:
        if os.path.exists(STORE_PATH):
            _load_from_disk(); return
        _STATE["pois"] = list(seed_pois)
        _rebuild_index(); _save_to_disk()

def load_all() -> List[Dict]:
    with _LOCK:
        if not _STATE["_index"]:
            _load_from_disk()
        return list(_STATE["pois"])

def get_poi(poi_id: str) -> Optional[Dict]:
    with _LOCK:
        if not _STATE["_index"]:
            _load_from_disk()
        p = _STATE["_index"].get(poi_id)
        return dict(p) if p else None

def upsert_poi(p: Dict):
    if "id" not in p: raise ValueError("poi must contain 'id'")
    with _LOCK:
        if not _STATE["_index"]:
            _load_from_disk()
        exists = _STATE["_index"].get(p["id"])
        if exists: exists.update(p)
        else:
            _STATE["pois"].append(p)
            _STATE["_index"][p["id"]] = p
        _save_to_disk()

# 简易检索：城市/类别精确 + 名称/别名模糊（大小写不敏感）

def search_pois(*, city: Optional[str]=None, name_like: Optional[str]=None,
                category: Optional[str]=None, limit: int=20) -> List[Dict]:
    with _LOCK:
        if not _STATE["_index"]:
            _load_from_disk()
        def like(v: Optional[str], pat: Optional[str]) -> bool:
            if pat is None: return True
            if v is None: return False
            return pat.lower() in str(v).lower()
        out = []
        for p in _STATE["pois"]:
            if city and p.get("city") != city: continue
            if category and p.get("category") != category: continue
            if name_like:
                # 命中主名或别名
                alias_list = p.get("aliases") or []
                if not (like(p.get("name"), name_like) or any(like(a, name_like) for a in alias_list)):
                    continue
            out.append(p)
        # 简单排序：热度 desc -> 名称长度 asc
        out.sort(key=lambda x: (-(x.get("popularity") or 0), len(x.get("name") or "")))
        return [dict(p) for p in out[:limit]]
