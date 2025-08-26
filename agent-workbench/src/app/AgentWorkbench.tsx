"use client";

import React, { useEffect, useMemo, useRef, useState, createContext, useContext, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { MapPin, Send, PlayCircle, Globe2, Wrench, Bot } from "lucide-react";
import { motion } from "framer-motion";
const API_BASE = "";
// 可选：地图库（Leaflet）。如果在你环境中不可用，可把 MapPane 替换成自定义的占位卡片。
// 预设假数据：北京基站（示例坐标非真实，仅用于 UI 演示）。
const demoStations = [
  { id: "BTS-001", name: "朝阳-望京2号站", lat: 39.9925, lng: 116.4747, vendor: "Huawei", band: "n78", status: "online" },
  { id: "BTS-002", name: "海淀-中关村西", lat: 39.9834, lng: 116.3162, vendor: "ZTE", band: "n41", status: "online" },
  { id: "BTS-003", name: "东城-王府井北", lat: 39.9166, lng: 116.4126, vendor: "Ericsson", band: "n1", status: "maintenance" },
  { id: "BTS-004", name: "丰台-丽泽东", lat: 39.8623, lng: 116.3005, vendor: "Nokia", band: "n28", status: "offline" },
];

/**
 * 事件总线（左右联动）
 */
const BusCtx = createContext({
  emit: (type, payload) => {},
  on: (type, handler) => {},
});

function useEventBus() {
  const handlers = useRef(new Map());
  const api = useMemo(
    () => ({
      emit: (type, payload) => {
        (handlers.current.get(type) || []).forEach((h) => h(payload));
      },
      on: (type, handler) => {
        if (!handlers.current.has(type)) handlers.current.set(type, []);
        handlers.current.get(type).push(handler);
        return () => {
          handlers.current.set(
            type,
            (handlers.current.get(type) || []).filter((f) => f !== handler)
          );
        };
      },
    }),
    []
  );
  return api;
}

/**
 * Chat 消息结构
 */
function ChatBubble({ role, content, meta }) {
  const isUser = role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <motion.div
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        className={`max-w-[85%] rounded-2xl px-4 py-2 shadow-sm text-sm leading-relaxed whitespace-pre-wrap ${
          isUser ? "bg-primary text-primary-foreground" : "bg-muted"
        }`}
      >
        {content}
        {meta?.suggest && (
          <div className="mt-2 text-xs opacity-80">💡{meta.suggest}</div>
        )}
      </motion.div>
    </div>
  );
}

/**
 * 左侧：聊天面板（手机比例）
 */


function ChatPane({ width = 420 }) {
  const bus = useContext(BusCtx);
  const [messages, setMessages] = useState([
    { role: "assistant", content: "你好，我是你的现场 Agent。你可以问：‘我想看看北京的基站’。" },
  ]);
  const [input, setInput] = useState("");
  const areaRef = useRef<HTMLDivElement | null>(null);

  const sendToAgent = useCallback(async (text: string, ctx?: any) => {
    if (!text.trim()) return;

    const trimmed = text.trim();
    if (/^\/?clear$/i.test(trimmed)) {
      const initial = [
        { role: "assistant", content: "你好，我是你的现场 Agent。你可以问：‘我想看看北京的基站’。" },
      ];
      setMessages(initial);
      setInput("");
      bus.emit("log:append", { channel: "cmd", message: "会话已清空（不影响记忆）" });
      return;
    }

    setMessages((m) => [...m, { role: "user", content: text }]);
    setInput("");

    if (/北京/.test(text) && /(基站|5G|站点)/.test(text)) {
      bus.emit("tool:map:load", { query: text, city: "北京" });
    }

    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: [...messages, { role: "user", content: text }],
        context: ctx || null,
      }),
    });

    if (!response.body) {
      setMessages((m) => [...m, { role: "assistant", content: "后端无响应（没有流）。" }]);
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let assistantBuf = "";

    const append = (delta: string) => {
      assistantBuf += delta;
      setMessages((m) => [...m, { role: "assistant", content: delta }]);
    };

    try {
      let leftover = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, { stream: true });
        const text = leftover + chunk;
        const lines = text.split("\n");
        leftover = lines.pop() || "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith("data:")) continue;
          const payload = trimmed.slice(5).trim();
          if (!payload) continue;

          const ev = JSON.parse(payload);
          if (ev.type === "token") {
            append(ev.delta || "");
          } else if (ev.type === "log") {
            bus.emit("log:append", { channel: ev.channel || "think", message: ev.message });
          }
        }
      }
    } catch (e) {
      setMessages((m) => [...m, { role: "assistant", content: `流式异常：${e}` }]);
    }
  }, [bus, messages, setMessages, setInput]);

  useEffect(() => {
    const off = bus.on("chat:ask-station", ({ station, question }) => {
      // 可选：上报“当前选中”
      fetch("/api/geo/selection", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ station_id: station.id, session_id: "demo" }),
      }).catch(()=>{});
  
      // 触发聊天，并把 station 当作上下文给后端
      sendToAgent(question, { station });     // ✅ 关键
    });
    return () => off && off();
  }, [bus, sendToAgent]);

  useEffect(() => {
    const off = bus.on("station:selected", (station) => {
      // 右侧点击 Marker → 左侧消息联动
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: `已选中基站【${station.name}】（${station.id}）。你想了解其覆盖、负载还是告警历史？`,
          meta: { suggest: "比如：‘它的覆盖半径是多少？’" },
        },
      ]);
    });
    return () => off && off();
  }, [bus]);


  
  return (
    <Card className="h-full w-full overflow-hidden rounded-2xl">
      <CardHeader className="p-4">
        <CardTitle className="flex items-center gap-2 text-base"><Bot size={18}/> Agent 对话</CardTitle>
      </CardHeader>
      <Separator />
      <CardContent className="p-0 h-full flex flex-col">
        <ScrollArea className="flex-1 p-4" ref={areaRef}>
          <div className="space-y-3">
            {messages.map((m, i) => (
              <ChatBubble key={i} role={m.role} content={m.content} meta={m.meta} />
            ))}
          </div>
        </ScrollArea>
        <div className="p-3 border-t flex items-center gap-2">
          <Input
            placeholder="提问：例如 ‘我想看看北京的基站’"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && sendToAgent(input)}
          />
          <Button onClick={() => sendToAgent(input)} className="gap-1">
            <Send size={16} /> 发送
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * 右侧：工具工作台（Tabs 切换 Map / Inspector / Logs）
 */
function ToolPane() {
  const bus = useContext(BusCtx);
  const [activeTab, setActiveTab] = useState("map");
  const [inspecting, setInspecting] = useState(null);
  const [mapQuery, setMapQuery] = useState(null);
  const [city, setCity] = useState("北京"); // ✅ 新增：当前城市
  const [logs, setLogs] = useState<Array<{channel:string; message:string; t:number}>>([]);

  useEffect(() => {
    const off1 = bus.on("tool:map:load", (payload) => { setActiveTab("map"); setMapQuery(payload); });
    const off2 = bus.on("tool:inspect", (data) => { setActiveTab("inspect"); setInspecting(data); });
    const off3 = bus.on("log:append", (log) => {
      setLogs((L) => [...L, { channel: log.channel || "info", message: log.message || String(log), t: Date.now() }]);
      // setActiveTab("log");
    });
    return () => { off1 && off1(); off2 && off2(); off3 && off3(); };
  }, [bus]);

  return (
    <Card className="h-full w-full rounded-2xl">
      <CardHeader className="p-4">
        <CardTitle className="flex items-center gap-2 text-base"><Wrench size={18}/> 工具工作台</CardTitle>
      </CardHeader>
      <Separator />
      <CardContent className="p-0">
        <Tabs value={activeTab} onValueChange={setActiveTab} className="w-full h-full">
          <div className="px-4 pt-3 flex items-center gap-2">
            <TabsList>
              <TabsTrigger value="map" className="gap-1"><Globe2 size={14}/> 地图</TabsTrigger>
              <TabsTrigger value="inspect">详情</TabsTrigger>
              <TabsTrigger value="rag">检索</TabsTrigger> {/* ✅ 新增 RAG Tab */}
              <TabsTrigger value="log">日志</TabsTrigger>
            </TabsList>

            {/* ✅ 城市选择器（简单版） */}
            <select
              value={city}
              onChange={(e)=> setCity(e.target.value)}
              className="ml-auto text-sm border rounded-md px-2 py-1"
              title="切换城市"
            >
              {["北京","上海","广州","深圳","杭州"].map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>

          <TabsContent value="map" className="p-4">
            <MapPane
              city={city}                       // ✅ 传 city
              query={mapQuery}
              onSelectStation={(s)=>{
                bus.emit("station:selected", s);
                bus.emit("tool:inspect", s);
              }}
            />
          </TabsContent>

          <TabsContent value="inspect" className="p-4">
            {inspecting ? <Inspector data={inspecting}/> : <Placeholder title="暂无选中" desc="从地图或检索结果中选择一个基站"/>}
          </TabsContent>

          <TabsContent value="rag" className="p-4">
            <RagPane
              city={city}
              onPick={(s)=>{
                // 点击检索结果 → 进入详情 & 联动聊天
                bus.emit("tool:inspect", s);
                bus.emit("station:selected", s);
                setActiveTab("inspect");
              }}
            />
          </TabsContent>

          <TabsContent value="log" className="p-4">
            <LogsPane logs={logs} />
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  );
}


function Placeholder({ title = "占位", desc = "" }) {
  return (
    <div className="border rounded-xl p-6 text-sm text-muted-foreground">
      <div className="font-medium mb-1">{title}</div>
      <div>{desc}</div>
    </div>
  );
}

/**
 * 地图面板：这里用占位 UI 来模拟（如要真地图，可接入 react-leaflet 或 MapboxGL）
 */
function MapPane({ city, query, onSelectStation }) {
  const [stations, setStations] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  const fetchStations = async () => {
    setLoading(true);
    try {
      const url = `${API_BASE}/api/geo/stations?` + new URLSearchParams({
        city,
        randomize: "1",
      }).toString();
      const res = await fetch(url);
      const data = await res.json();
      setStations(Array.isArray(data?.stations) ? data.stations : []);
    } catch (e) {
      console.error("load stations failed:", e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchStations(); }, [city, query]); // 城市/查询变化时都刷新

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-sm text-muted-foreground">城市：{city}（{loading ? "加载中…" : `共 ${stations.length} 站`}）</div>
        <div className="flex items-center gap-2">
          <Badge variant="secondary">{city}</Badge>
          <Button
            variant="outline"
            size="sm"
            title="将当前城市基站同步进 RAG"
            onClick={async ()=>{
              try {
                await fetch(`${API_BASE}/api/rag/geo/sync?city=${encodeURIComponent(city)}`, { method: "POST" });
                alert("已同步到 RAG");
              } catch {}
            }}
          >同步到 RAG</Button>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
        {stations.map((s) => (
          <button
            key={s.id}
            className="rounded-xl border p-3 text-left hover:shadow-sm transition"
            onClick={async () => {
              onSelectStation?.(s);
              // ✅ 上报“我选中了这个基站”
              fetch(`${API_BASE}/api/geo/selection`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ station_id: s.id, session_id: "demo" }) // 如有真实 session_id 换成你的
              }).catch(()=>{});
            }}
            title="点击查看详情并在聊天中继续询问"
          >
            <div className="flex items-center gap-2 font-medium text-sm"><MapPin size={14}/>{s.name}</div>
            <div className="text-xs mt-1 opacity-70">{s.id} · {s.vendor} · {s.band}</div>
            <div className="mt-2 text-xs">
              状态：<Badge className="ml-1" variant={s.status === 'online' ? 'default' : s.status === 'maintenance' ? 'secondary' : 'outline'}>
                {s.status}
              </Badge>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}


function Inspector({ data }) {
  const bus = useContext(BusCtx);
  const ask = (q) => bus.emit("chat:ask", q);
  return (
    <div className="space-y-3">
      <div className="text-sm text-muted-foreground">对象类型：基站</div>
      <div className="text-lg font-semibold">{data.name}</div>
      <div className="grid grid-cols-2 gap-2 text-sm">
        <div>ID：{data.id}</div>
        <div>厂商：{data.vendor}</div>
        <div>频段：{data.band}</div>
        <div>状态：{data.status}</div>
        <div>坐标：{data.lat.toFixed(4)}, {data.lng.toFixed(4)}</div>
        {data.desc && (
        <Card className="mt-2">
          <CardHeader className="p-3"><CardTitle className="text-sm">现场备注</CardTitle></CardHeader>
          <CardContent className="p-3 text-sm text-muted-foreground whitespace-pre-wrap">
            {data.desc}
          </CardContent>
        </Card>
      )}
      </div>
      <div className="flex gap-2">
        <Button variant="secondary" onClick={() => bus.emit("station:selected", data)}>在聊天中讨论</Button>
        <Button onClick={() => bus.emit("tool:run", { name: "coverage", args: { id: data.id } })}><PlayCircle size={14}/> 估算覆盖</Button>
      </div>
      <Card className="mt-2">
        <CardHeader className="p-3">
          <CardTitle className="text-sm">建议问题</CardTitle>
        </CardHeader>
        <CardContent className="p-3 text-sm">
          <div className="flex flex-wrap gap-2">
            {["它的id是多少？","坐标和厂商是什么？","现在状态是什么？"].map((q)=> (
              <Button key={q} size="sm" variant="outline" onClick={() => bus.emit("chat:ask-station", { station: data, question: q })}>{q}</Button>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function LogsPane({ logs = [] as Array<{channel: string; message: string; t: number}> }) {
  return (
    <div className="text-xs text-muted-foreground space-y-2">
      {logs.length === 0 ? (
        <div>暂无日志</div>
      ) : (
        logs.map((l, i) => (
          <div key={l.t + "-" + i}>
            · <span className="uppercase">{l.channel}</span>: {l.message}
          </div>
        ))
      )}
    </div>
  );
}
function RagPane({ city, onPick }:{ city:string; onPick:(station:any)=>void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  const search = async () => {
    if (!q.trim()) return;
    setLoading(true);
    try {
      // 可选：确保当前城市已同步（用户也可手动点 MapPane 的“同步到 RAG”）
      // await fetch(`${API_BASE}/api/rag/geo/sync?city=${encodeURIComponent(city)}`, { method: "POST" });

      const url = `${API_BASE}/api/rag/geo/search?` + new URLSearchParams({
        q,
        k: "8",
        city,
        min_score: "0.35",// ✅ 可调（与后端默认一致即可）
      }).toString();
      const res = await fetch(url);
      const data = await res.json();
      setHits(Array.isArray(data?.matches) ? data.matches : []);
    } catch (e) {
      console.error("rag search failed:", e);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        <Input placeholder={`在 ${city} 检索：例如 “n78 覆盖 望京”`} value={q}
          onChange={(e)=> setQ(e.target.value)}
          onKeyDown={(e)=> e.key === "Enter" && search()}
        />
        <Button onClick={search} disabled={loading}>{loading ? "检索中…" : "检索"}</Button>
      </div>

      <div className="space-y-2 text-sm">
        {hits.length === 0 ? (
          <div className="text-muted-foreground">暂无结果。先在地图页“同步到 RAG”，再试试“n78”“中关村”等关键词。</div>
        ) : hits.map((h, i) => (
          <Card key={h.chunk_id || i} className="p-3">
            <div className="text-xs opacity-60 mb-1">score: {h.score?.toFixed?.(3)}</div>
            <div className="font-medium">{h.title}</div>
            <div className="mt-1 text-xs whitespace-pre-wrap">{h.text}</div>
            {/* ✅ 如果有回填 station 的 desc，给一行摘要 */}
            {h.station?.desc && (
                <div className="mt-2 text-xs text-muted-foreground">现场备注：{h.station.desc}</div>
            )}
            <div className="mt-2 flex gap-2">
              <Button size="sm" variant="outline" onClick={()=> onPick(h.station)}>查看详情</Button>
              <Button size="sm" onClick={()=> onPick(h.station)}>在聊天中讨论</Button>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}



/**
 * 布局：左（手机宽度）⇄ 右（工具工作台），支持互动
 */
export default function AgentWorkbench() {
  const bus = useEventBus();

  useEffect(() => {
    const off = bus.on("chat:ask", (q) => {
      bus.emit("station:selected", { id: "BTS-001", name: "朝阳-望京2号站", suggest: q });
    });
    return () => off && off();
  }, [bus]);

  return (
    <BusCtx.Provider value={bus}>
      <div className="w-full h-full p-4">
        <div className="grid grid-cols-1 lg:grid-cols-[420px_minmax(0,1fr)] gap-4 h-[calc(100vh-2rem)]">
          {/* ❌ <ChatPane ref={chatRef}/> → ✅ <ChatPane/> */}
          <div className="h-full"><ChatPane/></div>
          <div className="h-full"><ToolPane/></div>
        </div>
      </div>
    </BusCtx.Provider>
  );
}
