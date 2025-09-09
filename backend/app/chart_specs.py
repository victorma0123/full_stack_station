# app/chart_specs.py
from collections import Counter
from typing import List, Dict, Tuple
import re
import math
import re
from collections import Counter

# --------- 生成 Plotly 规范的函数（返回 dict，可直接给前端） ---------

def spec_vendor_bar(rows: List[Dict], city: str) -> Tuple[str, Dict]:
    c = Counter([r.get("vendor","未知") for r in rows])
    x = list(c.keys()); y = [c[k] for k in x]
    spec = {
        "data": [ { "type": "bar", "x": x, "y": y, "name": "基站数" } ],
        "layout": { "title": { "text": f"{city} 各厂商基站数量" },
                    "margin": { "l": 40, "r": 10, "t": 40, "b": 40 },
                    "legend": { "orientation": "h" } },
        "config": { "displayModeBar": False }
    }
    return f"{city} 厂商分布（柱状图）", spec

def spec_status_pie(rows: List[Dict], city: str) -> Tuple[str, Dict]:
    labels = ["online","maintenance","offline"]
    values = [ sum(1 for r in rows if (r.get("status","").lower()==st)) for st in labels ]
    spec = {
        "data": [{ "type":"pie", "labels": labels, "values": values, "textinfo":"label+percent", "hole": 0 }],
        "layout": { "title": { "text": f"{city} 在线状态占比（饼图）" }, "margin": { "l":10,"r":10,"t":40,"b":10 } },
        "config": { "displayModeBar": False }
    }
    return f"{city} 在线状态占比（饼图）", spec

def spec_band_donut(rows: List[Dict], city: str) -> Tuple[str, Dict]:
    c = Counter([r.get("band","未知") for r in rows])
    labels = list(c.keys()); values = [c[k] for k in labels]
    spec = {
        "data": [{ "type":"pie", "labels": labels, "values": values, "hole": 0.5, "textinfo":"label+percent" }],
        "layout": { "title": { "text": f"{city} 频段占比（甜甜圈）" }, "margin": { "l":10,"r":10,"t":40,"b":10 } },
        "config": { "displayModeBar": False }
    }
    return f"{city} 频段占比（甜甜圈）", spec

def spec_status_stacked(rows: List[Dict], city: str) -> Tuple[str, Dict]:
    vendors = sorted(set((r.get("vendor") or "未知") for r in rows))
    statuses = ["online","maintenance","offline"]
    traces = []
    for st in statuses:
        traces.append({
            "type":"bar",
            "name": st,
            "x": vendors,
            "y": [ sum(1 for r in rows if (r.get("vendor") or "未知")==v and (r.get("status","").lower()==st))
                  for v in vendors ],
        })
    spec = {
        "data": traces,
        "layout": { "title": { "text": f"{city} 厂商×状态 分布（堆叠）" },
                    "barmode": "stack",
                    "margin": { "l": 50, "r": 10, "t": 40, "b": 50 },
                    "legend": { "orientation": "h" } },
        "config": { "displayModeBar": False }
    }
    return f"{city} 厂商×状态（堆叠柱）", spec

def spec_vendor_band_heatmap(rows: List[Dict], city: str) -> Tuple[str, Dict]:
    vendors = sorted(set((r.get("vendor") or "未知") for r in rows))
    bands   = sorted(set((r.get("band") or "未知") for r in rows))
    z = []
    for b in bands:
        z.append([ sum(1 for r in rows if (r.get("vendor") or "未知")==v and (r.get("band") or "未知")==b)
                  for v in vendors ])
    spec = {
        "data": [{ "type":"heatmap", "x": vendors, "y": bands, "z": z, "colorscale": "Blues" }],
        "layout": { "title": { "text": f"{city} 厂商×频段 热力图" }, "margin": { "l": 60, "r": 10, "t": 40, "b": 60 } },
        "config": { "displayModeBar": False }
    }
    return f"{city} 厂商×频段（热力图）", spec

def spec_status_bar_horizontal(rows: List[Dict], city: str) -> Tuple[str, Dict]:
    labels = ["online","maintenance","offline"]
    values = [ sum(1 for r in rows if (r.get("status","").lower()==st)) for st in labels ]
    spec = {
        "data": [{ "type":"bar", "orientation":"h", "x": values, "y": labels }],
        "layout": { "title": { "text": f"{city} 状态分布（水平条）" }, "margin": { "l": 60, "r": 10, "t": 40, "b": 40 } },
        "config": { "displayModeBar": False }
    }
    return f"{city} 状态分布（水平条）", spec

def spec_updated_at_hist(rows: List[Dict], city: str) -> Tuple[str, Dict]:
    xs = [ int(r.get("updated_at") or 0) for r in rows if r.get("updated_at") ]
    spec = {
        "data": [{ "type":"histogram", "x": xs, "nbinsx": 8 }],
        "layout": { "title": { "text": f"{city} 更新时间分布（直方图）" },
                    "xaxis": { "title": "epoch 秒" }, "yaxis": { "title": "数量" },
                    "margin": { "l": 50, "r": 10, "t": 40, "b": 45 } },
        "config": { "displayModeBar": False }
    }
    return f"{city} 更新时间（直方图）", spec

# --------- 根据关键词选择图表 ---------

VIS_HINT_RE = re.compile(
    r"(出图|可视化|图表|统计|分布|柱状|柱状图|饼图|甜甜圈|donut|折线|直方|热力|堆叠|条形|水平|横向|barh|hbar|bar|pie|line|hist|heatmap|stack)",
    re.I
)
def pick_spec(prompt: str, rows: List[Dict], city: str) -> Tuple[str, Dict]:
    p = prompt or ""
    if re.search(r"(甜甜圈|donut)", p, re.I):
        return spec_band_donut(rows, city)
    if re.search(r"(饼图|pie)", p, re.I):
        return spec_status_pie(rows, city)
    if re.search(r"(热力|heatmap)", p, re.I):
        return spec_vendor_band_heatmap(rows, city)
    if re.search(r"(堆叠|stack)", p, re.I):
        return spec_status_stacked(rows, city)
    if re.search(r"(直方|hist)", p, re.I):
        return spec_updated_at_hist(rows, city)
    if re.search(r"(水平|horizontal)", p, re.I):
        return spec_status_bar_horizontal(rows, city)
    # 默认：柱状
    return spec_vendor_bar(rows, city)
# app/chart_specs.py（文件末尾加）
def make_all_specs(rows, city):
    title1, spec1 = spec_vendor_bar(rows, city)
    title2, spec2 = spec_status_pie(rows, city)
    title3, spec3 = spec_band_donut(rows, city)
    title4, spec4 = spec_status_stacked(rows, city)
    title5, spec5 = spec_vendor_band_heatmap(rows, city)
    title6, spec6 = spec_status_bar_horizontal(rows, city)
    title7, spec7 = spec_updated_at_hist(rows, city)
    # 返回一个数组，每个元素都带上标题，前端好展示
    return [
        {"title": title1, "spec": spec1},
        {"title": title2, "spec": spec2},
        {"title": title3, "spec": spec3},
        {"title": title4, "spec": spec4},
        {"title": title5, "spec": spec5},
        {"title": title6, "spec": spec6},
        {"title": title7, "spec": spec7},
    ]

# === 3D 圆形：单基站“圆顶”覆盖（Gaussian Dome） ===
def spec_3d_station_dome(station: dict) -> tuple[str, dict]:
    name = station.get("name", "未知基站")
    band = (station.get("band") or "").lower()

    # 频段→半径（演示用近似值）
    base_r = {"n78": 600, "n41": 900, "n1": 1200, "n28": 2000}.get(band, 900)
    h0 = 10   # 地面基线高度
    dome_h = base_r * 0.35  # 圆顶最大高度
    sigma = base_r / 2.2    # 控制圆顶“坡度”

    # 生成网格（不依赖 numpy）
    N = 50
    xs = [ -base_r + 2*base_r*i/(N-1) for i in range(N) ]
    ys = [ -base_r + 2*base_r*j/(N-1) for j in range(N) ]
    Z  = []
    for y in ys:
        row = []
        for x in xs:
            r2 = x*x + y*y
            z  = h0 + (dome_h * math.exp(-r2/(2*sigma*sigma)))  # 高斯圆顶
            row.append(z)
        Z.append(row)

    # 外圈描边（圆），增强“圆形”观感
    circle_t = [2*math.pi*k/180 for k in range(0, 361, 3)]
    cx = [ base_r*math.cos(t) for t in circle_t ]
    cy = [ base_r*math.sin(t) for t in circle_t ]
    cz = [ h0 for _ in circle_t ]

    spec = {
        "data": [
            {
                "type": "surface",
                "x": xs, "y": ys, "z": Z,
                "colorscale": "YlGnBu",
                "showscale": False,
                "opacity": 0.95,
                # 轻量质感
                "lighting": { "ambient": 0.6, "diffuse": 0.7, "specular": 0.25, "roughness": 0.9 },
                "contours": {
                    "z": {"show": True, "usecolormap": True, "highlight": True, "project_z": True}
                },
            },
            {
                "type": "scatter3d", "mode": "lines",
                "x": cx, "y": cy, "z": cz,
                "line": {"width": 5},
                "name": "覆盖边界",
                "hoverinfo": "skip"
            },
            {
                "type": "scatter3d", "mode": "markers",
                "x": [0], "y": [0], "z": [h0 + dome_h + 3],
                "marker": {"size": 4},
                "name": name
            }
        ],
        "layout": {
            "title": {"text": f"{name} · 圆形覆盖示意"},
            "scene": {
                "xaxis": {"visible": False}, "yaxis": {"visible": False}, "zaxis": {"visible": False},
                "camera": {"eye": {"x": 1.7, "y": 1.6, "z": 1.2}},
                "aspectmode": "data",
            },
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)",
            "margin": {"l": 0, "r": 0, "t": 42, "b": 0},
        },
        "config": {"displayModeBar": False}
    }
    return f"{name} · 圆形覆盖（3D）", spec


# === 3D 圆形：整城“等高面密度场”（多站叠加） ===
def spec_3d_city_density_surface(rows: list[dict], city: str) -> tuple[str, dict]:
    if not rows:
        return f"{city} 3D 等高面", {"data": [], "layout": {"title": {"text": f"{city} 3D 等高面（无数据）"}}}

    # 以第一个点为近似投影基准，将经纬度投影到米（平面）
    lat0 = rows[0].get("lat", 0) or 0
    lng0 = rows[0].get("lng", 0) or 0
    def to_xy(lat, lng):
        dx = (lng - lng0) * 111320 * math.cos(math.radians(lat0))
        dy = (lat - lat0) * 110540
        return dx, dy

    # 频段→影响半径/高度；状态加权
    def band_radius(band: str) -> float:
        return {"n78": 600, "n41": 900, "n1": 1200, "n28": 2000}.get(band.lower(), 900)
    def band_amp(band: str) -> float:
        return {"n78": 0.9, "n41": 1.0, "n1": 1.1, "n28": 1.25}.get(band.lower(), 1.0)
    def status_w(st: str) -> float:
        s = (st or "").lower()
        return {"online": 1.0, "maintenance": 0.8, "offline": 0.4}.get(s, 0.7)

    # 计算范围
    pts = [to_xy(r.get("lat",0), r.get("lng",0)) for r in rows]
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    pad = 500
    xmin, xmax = min(xs)-pad, max(xs)+pad
    ymin, ymax = min(ys)-pad, max(ys)+pad

    # 生成网格
    N = 60
    gx = [ xmin + (xmax-xmin)*i/(N-1) for i in range(N) ]
    gy = [ ymin + (ymax-ymin)*j/(N-1) for j in range(N) ]
    gz = [[0.0 for _ in range(N)] for _ in range(N)]

    # 叠加每个站的“高斯圆顶”
    for r in rows:
        px, py = to_xy(r.get("lat",0), r.get("lng",0))
        rad   = band_radius(r.get("band",""))
        amp   = band_amp(r.get("band","")) * status_w(r.get("status",""))
        sigma = rad/2.3
        peak  = rad*0.33*amp  # 顶部高度
        for j, y in enumerate(gy):
            for i, x in enumerate(gx):
                dx = x - px; dy = y - py
                gz[j][i] += peak * math.exp(-(dx*dx + dy*dy)/(2*sigma*sigma))

    # 可选：叠加站点散点（增强参照）
    sx, sy, sz, stext = [], [], [], []
    for r in rows:
        px, py = to_xy(r.get("lat",0), r.get("lng",0))
        sx.append(px); sy.append(py); sz.append( max(0.0, 0.0) )
        stext.append(f"{r.get('name')} · {r.get('vendor')}/{r.get('band')} · {r.get('status')}")

    spec = {
        "data": [
            {
                "type": "surface",
                "x": gx, "y": gy, "z": gz,
                "colorscale": "YlGnBu",
                "showscale": False,
                "opacity": 0.96,
                "lighting": { "ambient": 0.55, "diffuse": 0.75, "specular": 0.25, "roughness": 0.9 },
                "contours": {
                    "z": {"show": True, "usecolormap": True, "highlight": True, "project_z": True}
                },
            },
            {
                "type": "scatter3d", "mode": "markers",
                "x": sx, "y": sy, "z": sz,
                "marker": {"size": 3, "opacity": 0.85},
                "text": stext, "hoverinfo": "text",
                "name": "基站"
            }
        ],
        "layout": {
            "title": {"text": f"{city} 基站覆盖等高面（3D 圆形）"},
            "scene": {
                "xaxis": {"visible": False}, "yaxis": {"visible": False}, "zaxis": {"title": "相对覆盖强度（示意）"},
                "camera": {"eye": {"x": 1.8, "y": 1.7, "z": 1.2}},
                "aspectmode": "data",
            },
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)",
            "margin": {"l": 0, "r": 0, "t": 42, "b": 0},
        },
        "config": {"displayModeBar": False}
    }
    return f"{city} 3D 等高面", spec
