

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
import { useMap } from "react-leaflet";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";


const PlotlyChart = dynamic(() => import("@/components/ui/PlotlyChart"), { ssr: false });
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";
/**
 * 事件总线（左右联动）
 */
const BusCtx = createContext({
  emit: (type: any, payload: any) => {},
  on: (type: any, handler: any) => {},
});

function useEventBus() {
  const handlers = useRef(new Map());
  const api = useMemo(
    () => ({
      emit: (type: any, payload: any) => {
        (handlers.current.get(type) || []).forEach((h: (arg0: any) => any) => h(payload));
      },
      on: (type: any, handler: any) => {
        if (!handlers.current.has(type)) handlers.current.set(type, []);
        handlers.current.get(type).push(handler);
        return () => {
          handlers.current.set(
            type,
            (handlers.current.get(type) || []).filter((f: any) => f !== handler)
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
  const isRouter = meta?.channel === "router";

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <motion.div
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        className={`max-w-[85%] rounded-2xl px-4 py-2 shadow-sm text-sm leading-relaxed ${
          isUser ? "bg-primary text-primary-foreground" : "bg-muted"
        }`}
      >
        <div
          className={`prose prose-sm max-w-none ${
            isRouter ? "prose-router" : ""
          }`}
        >
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
    
            components={{
              table: ({ node, ...props }) => (
                <table
                  className="table-fixed w-full border border-border text-sm my-3"
                  {...props}
                />
              ),
              thead: ({ node, ...props }) => (
                <thead className="bg-muted/60" {...props} />
              ),
              th: ({ node, ...props }) => (
                <th
                  className="border border-border px-6 py-2 text-left font-medium"
                  {...props}
                />
              ),
              td: ({ node, ...props }) => (
                <td
                  className="border border-border px-6 py-2 align-top"
                  {...props}
                />
              ),
              code: ({ inline, className, children, ...props }) =>
                inline ? (
                  <code
                    className="font-mono px-1 py-0.5 bg-muted rounded"
                    {...props}
                  >
                    {children}
                  </code>
                ) : (
                  <code
                    className="block font-mono text-xs p-3 bg-muted rounded overflow-x-auto"
                    {...props}
                  >
                    {children}
                  </code>
                ),
              h1: ({ node, ...props }) => (
                <h1 className="text-lg font-semibold mt-4 mb-2" {...props} />
              ),
              h2: ({ node, ...props }) => (
                <h2 className="text-base font-semibold mt-3 mb-1.5" {...props} />
              ),
              ul: ({ node, ...props }) => (
                <ul className="list-disc pl-5 space-y-1" {...props} />
              ),
              ol: ({ node, ...props }) => (
                <ol className="list-decimal pl-5 space-y-1" {...props} />
              ),
              li: ({ node, ...props }) => (
                <li className="leading-relaxed" {...props} />
              ),
              a: ({ node, ...props }) => (
                <a
                  {...props}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-blue-600 underline"
                />
              ),
            }}
          >
            {content}
          </ReactMarkdown>
        </div>

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


function ChatPane({ width = 420, height = 720, fill = false }: { width?: number; height?: number; fill?: boolean }) {
  const bus = useContext(BusCtx);
  const [streamActive, setStreamActive] = useState(false);
  const assistantIndexRef = useRef<number>(-1);
  const controllerRef = useRef<AbortController | null>(null);
  // 追踪 think 模式与缓冲
  const inThinkXmlRef   = useRef<boolean>(false);   // <think> ... </think>
  const inThinkFenceRef = useRef<boolean>(false);   // ```think / ```thought / ```reasoning
  const thinkBufRef     = useRef<string>("");       // 累积 think 文本

  const lastAssistantTextRef = useRef<string>(""); // 记录最新助手文本（给结束时解析 plotly）


  const [messages, setMessages] = useState([
    { role: "assistant", content: "你好，我是你的现场 Agent。你可以问：‘我想看看北京的基站’。" },
  ]);
  const [input, setInput] = useState("");
  const stripThinkAndLog = useCallback((raw: string) => {
    let s = raw;
  
    // ===== 1) XML 风格 <think>...</think> =====
    if (inThinkXmlRef.current) {
      const end = s.toLowerCase().indexOf("</think>");
      if (end >= 0) {
        // 收尾：把 think 内的本片内容入缓冲，然后退出 think
        thinkBufRef.current += s.slice(0, end);
        s = s.slice(end + "</think>".length);
        inThinkXmlRef.current = false;
  
        const t = thinkBufRef.current.replace(/\s+/g, " ").trim();
        if (t) bus.emit("log:append", { channel: "think", message: t.length > 240 ? t.slice(0, 240) + "…" : t });
        thinkBufRef.current = "";
      } else {
        // 整个分片都在 think 内：累计并吞掉
        thinkBufRef.current += s;
        return "";
      }
    }
    // 本分片可能出现新的 <think>
    while (true) {
      const start = s.toLowerCase().indexOf("<think>");
      if (start < 0) break;
      const end = s.toLowerCase().indexOf("</think>", start + 7);
      if (end >= 0) {
        // 同片开闭：提取日志并删除
        const inner = s.slice(start + 7, end);
        const t = inner.replace(/\s+/g, " ").trim();
        if (t) bus.emit("log:append", { channel: "think", message: t.length > 240 ? t.slice(0, 240) + "…" : t });
        s = s.slice(0, start) + s.slice(end + "</think>".length);
      } else {
        // 只开未闭：进入 think 状态，截断后续
        inThinkXmlRef.current = true;
        thinkBufRef.current += s.slice(start + 7);
        s = s.slice(0, start);
        break;
      }
    }
  
    // ===== 2) 围栏 ```think / ```thought / ```reasoning =====
    const fenceOpen  = /```(?:think|thought|reasoning)\b/i;
    const fenceClose = /```/;
  
    if (inThinkFenceRef.current) {
      const mClose = s.match(fenceClose);
      if (mClose) {
        thinkBufRef.current += s.slice(0, mClose.index!);
        s = s.slice(mClose.index! + mClose[0].length);
        inThinkFenceRef.current = false;
  
        const t = thinkBufRef.current.replace(/\s+/g, " ").trim();
        if (t) bus.emit("log:append", { channel: "think", message: t.length > 240 ? t.slice(0, 240) + "…" : t });
        thinkBufRef.current = "";
      } else {
        thinkBufRef.current += s;
        return "";
      }
    }
    while (true) {
      const mOpen = s.match(fenceOpen);
      if (!mOpen) break;
      const from = mOpen.index! + mOpen[0].length;
      const rest = s.slice(from);
      const mClose = rest.match(fenceClose);
      if (mClose) {
        const inner = rest.slice(0, mClose.index!);
        const t = inner.replace(/\s+/g, " ").trim();
        if (t) bus.emit("log:append", { channel: "think", message: t.length > 240 ? t.slice(0, 240) + "…" : t });
        // 删除整段围栏内容
        s = s.slice(0, mOpen.index!) + rest.slice(mClose.index! + mClose[0].length);
      } else {
        inThinkFenceRef.current = true;
        thinkBufRef.current += rest;
        s = s.slice(0, mOpen.index!);
        break;
      }
    }
  
    return s;
  }, [bus]);
  

  const endRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const id = requestAnimationFrame(() => {
      endRef.current?.scrollIntoView({ block: "end" });
    });
    return () => cancelAnimationFrame(id);
  }, [messages]);

  const sendToAgent = useCallback(async (text: string, ctx?: any) => {
    const ask = text.trim();
    if (!ask) return;
  
    // 并发保护：若正在流式输出，先打断旧流
    if (streamActive && controllerRef.current) {
      try { controllerRef.current.abort(); } catch {}
      controllerRef.current = null;
      setStreamActive(false);
    }
  
    // /clear
    if (/^\/?clear$/i.test(ask)) {
      const initial = [
        { role: "assistant", content: "你好，我是你的现场 Agent。你可以问：‘我想看看北京的基站’。" },
      ];
      setMessages(initial);
      setInput("");
      bus.emit("log:append", { channel: "cmd", message: "会话已清空（不影响记忆）" });
      return;
    }
  
    // 用户消息入列
    const userMsg = { role: "user" as const, content: ask };
    setMessages((m) => [...m, userMsg]);
    setInput("");
  
    // 联动地图（可留）
    if (/北京/.test(ask) && /(基站|5G|站点)/.test(ask)) {
      bus.emit("tool:map:load", { query: ask, city: "北京" });
    }
  
    // 稳定快照（避免异步 setState 造成丢历史）
    const convo = [...messages, userMsg];
  
    // 先插入“空气泡”，并记录索引到 ref
    setMessages((m) => {
      assistantIndexRef.current = m.length;
      lastAssistantTextRef.current = "";
      return [...m, { role: "assistant" as const, content: "", meta: {} }];
    });
  
    // 开始流
    const ac = new AbortController();
    controllerRef.current = ac;
    setStreamActive(true);
  
    try {
      const response = await fetch(`${API_BASE}/api/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ messages: convo, context: ctx || null }),
        signal: ac.signal,
        // 这三项能避免浏览器/中间层缓冲
        cache: "no-store",
        keepalive: false,
      });
  
      if (!response.body) {
        throw new Error("后端无响应（没有流 body）");
      }
  
      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
  
      // —— SSE 解析：按“空行”切事件，忽略心跳行（以冒号起始）
      let buf = "";
      const flushEvent = (evt: string) => {
        // 只处理 data: 行
        const lines = evt.split("\n").map(l => l.trim()).filter(Boolean);
        for (const line of lines) {
          if (line.startsWith(":")) continue; // 心跳
          if (!line.startsWith("data:")) continue;
          const jsonStr = line.slice(5).trim();
          if (!jsonStr) continue;
  
          let ev: any;
          try { ev = JSON.parse(jsonStr); } catch { continue; }
  
          if (ev.type === "start") return;
  
          if (ev.type === "token") {
            const clean = stripThinkAndLog(ev.delta || "");
            if (!clean) return; // 这片要么在 think 内，要么被完全剥离
          
            const i = assistantIndexRef.current;
            lastAssistantTextRef.current += clean; // ← 新增
            setMessages((m) => {
              if (i < 0 || i >= m.length) return m;
              const copy = m.slice();
              copy[i] = { ...(copy[i] as any), content: (copy[i] as any).content + clean };
              return copy;
            });
            return;
          }
          if (ev.type === "tool" && ev.tool === "plotly") {
            bus.emit("charts:show", { spec: ev.spec, specs: ev.specs, title: ev.title || "AI 生成图表" });
            return;
          }
          if (ev.type === "tool" && ev.tool === "plotly_batch") {
            const items = Array.isArray(ev.items) ? ev.items : [];
            bus.emit("charts:show-batch", { items, title: ev.title || "图表总览" });
            return;
          }
          
          
  
          if (ev.type === "log") {
            bus.emit("log:append", { channel: ev.channel || "think", message: ev.message });
            if (ev.channel === "router") {
              const i = assistantIndexRef.current;
              setMessages((m) => {
                if (i < 0 || i >= m.length) return m;
                const copy = m.slice();
                copy[i] = {
                  ...(copy[i] as any),
                  meta: { ...((copy[i] as any).meta || {}), channel: "router" },
                };
                return copy;
              });
            }
            return;
          }
  
          if (ev.type === "end") {
            // 结束前，尝试从最后助手文本里解析 ```plotly 代码块
            try {
              const text = lastAssistantTextRef.current || "";
              const m = text.match(/```plotly\s*([\s\S]*?)```/i);
              if (m) {
                const spec = JSON.parse(m[1]);
                bus.emit("charts:show", { spec, title: "AI 生成图表" });
              }
            } catch {}
            // 正常结束
            throw new DOMException("done", "AbortError");
          }
        }
      };
  
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
  
        // 事件以空行分割：\n\n
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const evt = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          flushEvent(evt);
        }
      }
    } catch (err: any) {
      // “正常结束”走了 AbortError(done)，不要报错
      if (!(err instanceof DOMException && err.name === "AbortError")) {
        setMessages((m) => [...m, { role: "assistant", content: `流式异常：${String(err?.message || err)}` }]);
      }
    } finally {
      setStreamActive(false);
      controllerRef.current = null;
      requestAnimationFrame(() => endRef.current?.scrollIntoView({ block: "end" }));
    }
  }, [bus, messages, streamActive]);
  
  useEffect(() => {
    const off = bus.on("chat:ask-station", ({ station, question }) => {
      fetch("/api/geo/selection", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ station_id: station.id, session_id: "demo" }),
      }).catch(()=>{});
      sendToAgent(question, { station });
    });
    return () => off && off();
  }, [bus, sendToAgent]);

  useEffect(() => {
    const off = bus.on("station:selected", (station: { name: any; id: any; }) => {
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

  useEffect(() => {
    return () => {
      try { controllerRef.current?.abort(); } catch {}
    };
  }, []);
  

  return (
    <Card 
      className="overflow-hidden rounded-3xl h-full" style={{ height: fill ? "100%" : height, width: fill ? "100%" : width, maxWidth: "100%" }}>
      <CardHeader className="p-4">
        <CardTitle className="flex items-center gap-2 text-base"><Bot size={18}/> Agent 对话</CardTitle>
      </CardHeader>
      <Separator />
      <CardContent className="p-0 h-full flex flex-col min-h-0">
        <div className="flex-1 min-h-0 flex flex-col">
          <ScrollArea className="flex-1 min-h-0 px-4 py-3 overflow-y-auto">
            <div className="space-y-3">
              {messages.map((m, i) => (
                <ChatBubble key={i} role={m.role} content={m.content} meta={m.meta} />
              ))}
              <div ref={endRef} />
            </div>
          </ScrollArea>
          <div className="p-3 border-t flex items-center gap-2 bg-background">
          <Input
            placeholder="提问：例如 ‘我想看看北京的基站’"
            value={input}
            disabled={streamActive}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !streamActive && sendToAgent(input)}
          />
          <Button onClick={() => sendToAgent(input)} className="gap-1" disabled={streamActive}>
            <Send size={16} /> {streamActive ? "生成中…" : "发送"}
          </Button>
          </div>
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
  const [city, setCity] = useState("北京");
  const [logs, setLogs] = useState<Array<{ channel: string; message: string; t: number }>>([]);
  const [coverageReq, setCoverageReq] = useState<{ station_id: string } | null>(null);
  const [chartItems, setChartItems] = useState<Array<{ title: string; spec: any }>>([]);

  // 图表区滚动容器，用于切到“图表”时回到顶部
  const chartsAreaRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const off1 = bus.on("tool:map:load", (payload: any) => {
      setActiveTab("map");
      setMapQuery(payload);
    });
    const off2 = bus.on("tool:inspect", (data: any) => {
      setActiveTab("inspect");
      setInspecting(data);
    });
    const off3 = bus.on("log:append", (log: { channel: any; message: any }) => {
      setLogs((L) => [...L, { channel: log.channel || "info", message: log.message || String(log), t: Date.now() }]);
    });
    const off4 = bus.on("tool:run", (req: { name: string; args: { id: any } }) => {
      if (req?.name === "coverage" && req?.args?.id) {
        setCoverageReq({ station_id: req.args.id });
        setActiveTab("coverage");
      }
    });
    const off5 = bus.on("charts:show", ({ spec, specs, title }) => {
      const items = Array.isArray(specs)
        ? specs
        : spec
        ? [{ title: title || "AI 生成图表", spec }]
        : [];
      setChartItems(items);
      setActiveTab("charts");
    });
    const off6 = bus.on("charts:show-batch", ({ items }) => {
      setChartItems(items);
      setActiveTab("charts");
    });

    return () => {
      off1 && off1();
      off2 && off2();
      off3 && off3();
      off4 && off4();
      off5 && off5();
      off6 && off6();
    };
  }, [bus]);

  // 切换到“图表”或图表内容变化时，将滚动置顶
  useEffect(() => {
    if (activeTab !== "charts") return;
    const root = chartsAreaRef.current as HTMLDivElement | null;
    const viewport = root?.querySelector('[data-radix-scroll-area-viewport]') as HTMLDivElement | null;
    if (viewport) viewport.scrollTop = 0;
  }, [activeTab, chartItems]);

  return (
    <Card className="h-full w-full rounded-2xl">
      <CardHeader className="p-4">
        <CardTitle className="flex items-center gap-2 text-base">
          <Wrench size={18} /> 工具工作台
        </CardTitle>
      </CardHeader>
      <Separator />
      <CardContent className="p-0 h-full flex flex-col min-h-0">
        <Tabs value={activeTab} onValueChange={setActiveTab} className="w-full h-full flex flex-col">
          {/* 顶部：固定不压缩 */}
          <div className="px-4 pt-3 flex items-center gap-2 shrink-0">
            <TabsList>
              <TabsTrigger value="map" className="gap-1">
                <Globe2 size={14} /> 地图
              </TabsTrigger>
              <TabsTrigger value="inspect">详情</TabsTrigger>
              <TabsTrigger value="search">检索</TabsTrigger>
              <TabsTrigger value="coverage" className="p-4 h-full">
                覆盖
              </TabsTrigger>
              <TabsTrigger value="log">日志</TabsTrigger>
              <TabsTrigger value="charts">图表</TabsTrigger>
            </TabsList>

            {/* 城市选择器 */}
            <select
              value={city}
              onChange={(e) => setCity(e.target.value)}
              className="ml-auto text-sm border rounded-md px-2 py-1"
              title="切换城市"
            >
              {["北京", "上海", "广州", "深圳", "杭州"].map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>

          {/* 内容容器：占满剩余空间并允许内部滚动 */}
          <div className="flex-1 min-h-0">
            <TabsContent value="map" className="h-full p-4">
              <MapPane
                city={city}
                query={mapQuery}
                onSelectStation={(s: any) => {
                  bus.emit("station:selected", s);
                  bus.emit("tool:inspect", s);
                }}
              />
            </TabsContent>

            <TabsContent value="coverage" className="h-full p-4">
              {coverageReq ? (
                <CoveragePane request={coverageReq} />
              ) : (
                <Placeholder title="暂无覆盖图" desc="在详情中点击‘估算覆盖’按钮以查看覆盖范围" />
              )}
            </TabsContent>

            <TabsContent value="inspect" className="h-full p-4">
              {inspecting ? (
                <Inspector data={inspecting} />
              ) : (
                <Placeholder title="暂无选中" desc="从地图或检索结果中选择一个基站" />
              )}
            </TabsContent>

            <TabsContent value="search" className="h-full p-4">
              <DbSearchPane
                onPick={(s) => {
                  bus.emit("tool:inspect", s);
                  bus.emit("station:selected", s);
                  setActiveTab("inspect");
                }}
              />
            </TabsContent>

            <TabsContent value="log" className="h-full p-4">
              <LogsPane logs={logs} />
            </TabsContent>

            {/* 图表：内部滚动，不撑高外层 */}
            <TabsContent value="charts" className="h-full p-0">
              <ScrollArea ref={chartsAreaRef} className="h-full">
                <div className="p-4">
                  {chartItems.length > 0 ? (
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      {chartItems.map((it, idx) => (
                        <Card key={idx} className="p-3">
                          <div className="text-sm font-medium mb-2">{it.title}</div>
                          <div className="h-72 border rounded-xl p-2">
                            <PlotlyChart {...it.spec} />
                          </div>
                        </Card>
                      ))}
                    </div>
                  ) : (
                    <Placeholder title="暂无图表" desc="在聊天里说：‘北京全部图’ 或 ‘北京饼图’ 等。" />
                  )}
                </div>
              </ScrollArea>
            </TabsContent>
          </div>
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
      const url = `${API_BASE}/api/geo/stations?` + new URLSearchParams({ city }).toString();
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


function Inspector({ data }: { data: any }) {
  const bus = useContext(BusCtx);
  return (
    <div className="space-y-3">
      <div className="text-sm text-muted-foreground">对象类型：基站</div>

      <div className="text-lg font-semibold">{data.name}</div>

      {/* 基本信息：两列网格 */}
      <div className="grid grid-cols-2 gap-2 text-sm">
        <div>ID：{data.id}</div>
        <div>厂商：{data.vendor}</div>
        <div>频段：{data.band}</div>
        <div>状态：{data.status}</div>
        <div>坐标：{Number(data.lat).toFixed(4)}, {Number(data.lng).toFixed(4)}</div>
      </div>

      {/* 备注卡片：移出 grid，单独一块 */}
      {data.desc && (
        <Card className="mt-2">
          <CardHeader className="p-3">
            <CardTitle className="text-sm">现场备注</CardTitle>
          </CardHeader>
          <CardContent className="p-3 text-sm text-muted-foreground whitespace-pre-wrap">
            {data.desc}
          </CardContent>
        </Card>
      )}

      {/* 行为按钮 */}
      <div className="flex gap-2">
        <Button
          variant="secondary"
          onClick={() => {
            bus.emit("station:selected", data); // 保留联动提示
            bus.emit("chat:ask-station", { station: data, question: "它的id是多少？" });
          }}
        >
          在聊天中讨论
        </Button>
        <Button onClick={() =>bus.emit("tool:run", { name: "coverage", args: { id: data.id } })}>
          <PlayCircle size={14} /> 估算覆盖
        </Button>
      </div>

      {/* 建议问题 */}
      <Card className="mt-2">
        <CardHeader className="p-3">
          <CardTitle className="text-sm">建议问题</CardTitle>
        </CardHeader>
        <CardContent className="p-3 text-sm">
          <div className="flex flex-wrap gap-2">
            {["它的id是多少？", "坐标和厂商是什么？", "现在状态是什么？"].map((q) => (
              <Button
                key={q}
                size="sm"
                variant="outline"
                onClick={() => bus.emit("chat:ask-station", { station: data, question: q })}
              >
                {q}
              </Button>
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
function DbSearchPane({ onPick }:{ onPick:(station:any)=>void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  const search = async () => {
    if (!q.trim()) return;
    setLoading(true);
    try {
      // 可选：确保当前城市已同步（用户也可手动点 MapPane 的“同步到 RAG”）
      // await fetch(`${API_BASE}/api/rag/geo/sync?city=${encodeURIComponent(city)}`, { method: "POST" });

      const url = `${API_BASE}/api/db/stations/search?` + new URLSearchParams({
        q, k: "12"}).toString();
      const res = await fetch(url);
      const data = await res.json();
      setHits(Array.isArray(data?.matches) ? data.matches : []);
    } catch (e) {
      console.error("db search failed:", e);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        <Input placeholder={`检索：ID/站名/城市/厂商/频段/状态/备注，如 “BJS-003” “望京 n78 在线”`} value={q}
          onChange={(e)=> setQ(e.target.value)}
          onKeyDown={(e)=> e.key === "Enter" && search()}
        />
        <Button onClick={search} disabled={loading}>{loading ? "检索中…" : "检索"}</Button>
      </div>

      <div className="space-y-2 text-sm">
        {hits.length === 0 ? (
          <div className="text-muted-foreground">暂无结果。可尝试输入站名、ID 或城市内的关键词。</div>
        ) : hits.map((h, i) => (
          <Card key={h.id || i} className="p-3">
            <div className="font-medium">{h.name}（{h.id}）</div>
            <div className="mt-1 text-xs whitespace-pre-wrap">
              {h.city} · {h.vendor} · {h.band} · 状态：{h.status}<br/>
              坐标：{Number(h.lat).toFixed?.(6)}, {Number(h.lng).toFixed?.(6)}
              {h.desc ? <><br/>现场备注：{h.desc}</> : null}
            </div>
            <div className="mt-2 flex gap-2">
            <Button size="sm" variant="outline" onClick={()=> onPick(h)}>查看详情</Button>
            <Button size="sm" onClick={()=> onPick(h)}>在聊天中讨论</Button>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}


import dynamic from "next/dynamic";
const LeafletMap = dynamic(() => import("react-leaflet").then(m => m.MapContainer), { ssr: false });
const TileLayer = dynamic(() => import("react-leaflet").then(m => m.TileLayer), { ssr: false });
const Marker = dynamic(() => import("react-leaflet").then(m => m.Marker), { ssr: false });
const Circle = dynamic(() => import("react-leaflet").then(m => m.Circle), { ssr: false });
const Popup = dynamic(() => import("react-leaflet").then(m => m.Popup), { ssr: false });
function ResizeOnShow() {
  const map = useMap();

  React.useEffect(() => {
    if (!map) return;

    let alive = true;
    const safeInvalidate = () => {
      // 1) 组件还在  2) map._mapPane 已创建（Leaflet 初始化完成）
      // @ts-ignore
      if (!alive || !map || !(map as any)._mapPane) return;
      try {
        map.invalidateSize();
      } catch {}
    };

    // 优先等待 Leaflet 就绪再触发
    map.whenReady(() => {
      // 第一帧
      requestAnimationFrame(safeInvalidate);
      // 第二帧兜底（有些布局需要两帧）
      requestAnimationFrame(() => requestAnimationFrame(safeInvalidate));
    });

    // 再监听一次 'load'（某些瓦片/样式异步时更稳）
    const onLoad = () => requestAnimationFrame(safeInvalidate);
    // @ts-ignore
    map.on("load", onLoad);

    return () => {
      alive = false;
      // @ts-ignore
      map.off("load", onLoad);
    };
  }, [map]);

  return null;
}

// 放在文件中（与 ResizeOnShow 同级）
function UseResizeInvalidate({ mapRef }: { mapRef: React.MutableRefObject<any> }) {
  const boxRef = React.useRef<HTMLDivElement | null>(null);

  React.useEffect(() => {
    if (!boxRef.current) return;
    let alive = true;

    const safeInvalidate = () => {
      const map = mapRef.current;
      // @ts-ignore
      if (!alive || !map || !(map as any)._mapPane) return;
      try { map.invalidateSize(); } catch {}
    };

    const ro = new ResizeObserver(() => {
      requestAnimationFrame(safeInvalidate);
    });
    ro.observe(boxRef.current);

    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          requestAnimationFrame(() => {
            requestAnimationFrame(safeInvalidate);
          });
        }
      },
      { threshold: 0.1 }
    );
    io.observe(boxRef.current);

    return () => { alive = false; ro.disconnect(); io.disconnect(); };
  }, [mapRef]);

  return (props: { className?: string; children: React.ReactNode }) => (
    <div ref={boxRef} className={props.className}>{props.children}</div>
  );
}


if (typeof window !== "undefined") {
  const L = require("leaflet");
  // 通过构建工具解析资源路径
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const iconUrl = require("leaflet/dist/images/marker-icon.png");
  const iconRetinaUrl = require("leaflet/dist/images/marker-icon-2x.png");
  const shadowUrl = require("leaflet/dist/images/marker-shadow.png");
  // @ts-ignore
  delete L.Icon.Default.prototype._getIconUrl;
  L.Icon.Default.mergeOptions({ iconRetinaUrl, iconUrl, shadowUrl });
}

// ✅ 安全壳：避免同一容器被重复初始化（StrictMode / Tab 切换 / HMR）
// ✅ 改造后的 SafeLeaflet
function SafeLeaflet({ id = "leaflet-wrapper", mapRef, children, ...rest }: any) {
  const [ready, setReady] = React.useState(false);
  const wrapperRef = React.useRef<HTMLDivElement | null>(null);
  const [containerKey, setContainerKey] = React.useState(() => `${id}-${Date.now()}`);

  React.useLayoutEffect(() => {
    const el = wrapperRef.current;
    if (!el) return;

    // 1) 若已有 map 实例，先彻底销毁
    try { if (mapRef?.current) { mapRef.current.remove(); mapRef.current = null; } } catch {}

    // 2) 清空掉 wrapper 内所有 leaflet 残留
    try {
      const existed = el.querySelectorAll(".leaflet-container");
      existed.forEach(node => node.parentNode && node.parentNode.removeChild(node));
      // 保险：直接清空子节点
      el.replaceChildren();
    } catch {}

    // 3) 用新的 key 强制下一次渲染创建全新容器
    setContainerKey(`${id}-${performance.now()}`);

    const raf = requestAnimationFrame(() => setReady(true));
    return () => cancelAnimationFrame(raf);
  }, [id]);

  return (
    <div id={id} ref={wrapperRef} style={{ height: "100%", width: "100%" }}>
      {ready ? (
        <LeafletMap
          key={containerKey}             // ✅ 每次都用全新容器
          whenCreated={(map: any) => {
            // 避免残留引用
            if (mapRef) {
              if (mapRef.current && mapRef.current !== map) {
                try { mapRef.current.remove(); } catch {}
              }
              mapRef.current = map;
            }
          }}
          {...rest}
        >
          {children}
        </LeafletMap>
      ) : null}
    </div>
  );
}




function CoveragePane({ request }: { request: { station_id: string } }) {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<null | { station?: any; radius_m?: number; address?: string }>(null);
  const [err, setErr] = useState<string | null>(null);

  const mapRef = React.useRef<any>(null);
  const Box = UseResizeInvalidate({ mapRef });

  // 仅在客户端渲染
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const isClient = typeof window !== "undefined";

  // 每次换站点都强制换一个全新的实例 key
  const [instanceKey, setInstanceKey] = useState(0);
  useEffect(() => { setInstanceKey(k => k + 1); }, [request?.station_id]);

  // 卸载时彻底销毁旧 Map 实例
  useEffect(() => {
    return () => {
      if (mapRef.current) {
        try { mapRef.current.remove(); } catch {}
        mapRef.current = null;
      }
    };
  }, []);

  const fetchCoverage = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const url = `${API_BASE}/api/geo/coverage?` + new URLSearchParams({ station_id: request.station_id }).toString();
      const res = await fetch(url);
      const json = await res.json();
      if (!json?.ok) throw new Error(json?.error || "coverage api failed");
      setData(json);
    } catch (e: any) {
      setErr(e?.message ?? String(e));
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [request?.station_id]);

  useEffect(() => { fetchCoverage(); }, [fetchCoverage]);

  if (loading) return <div className="text-sm text-muted-foreground">覆盖估算中…</div>;
  if (err) return (
    <div className="text-sm text-red-500">
      加载失败：{err}
      <Button size="sm" variant="outline" className="ml-2" onClick={fetchCoverage}>重试</Button>
    </div>
  );
  if (!data?.station) return <div className="border rounded-xl p-4 text-sm">暂无数据或站点信息</div>;
  if (!isClient || !mounted) return <div className="border rounded-xl p-4 text-sm">地图初始化中…</div>;

  const s = data.station;
  const lat = Number(s?.lat ?? 0);
  const lng = Number(s?.lng ?? 0);
  const radius = Number(data?.radius_m ?? 0);
  const zoom = radius > 0 ? Math.max(12, Math.min(17, Math.floor(15 - Math.log2(radius / 500)))) : 14;

  // 为 MapContainer 指定一个稳定且唯一的 id，便于 SafeLeaflet 做兜底清理
  const containerId = `leaflet-map-${request?.station_id || "unknown"}-${instanceKey}`;

  return (
    <div className="space-y-3">
      <div className="text-sm text-muted-foreground">
        覆盖为启发式估算，仅供参考。{data?.address ? ` 地址：${data.address}` : ""}
      </div>

      <div className="relative" style={{ transform: "none", isolation: "isolate", height: "100%", width: "100%" }}>
        <Box className="h-80 md:h-96 min-h-[320px] w-full rounded-xl overflow-hidden border">
          <SafeLeaflet
            id={containerId}          // ✅ 让容器可被检测/清理
            key={containerId}         // ✅ 每次都是真正新的实例
            mapRef={mapRef}
            center={[lat, lng]}
            zoom={zoom}
            style={{ height: "100%", width: "100%" }}
            zoomAnimation={false}
            fadeAnimation={false}
            preferCanvas
          >
            <TileLayer
              url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
              attribution="&copy; OpenStreetMap"
              detectRetina
              eventHandlers={{
                load: () => {
                  const m = mapRef.current as any;
                  if (m && m._mapPane) requestAnimationFrame(() => m.invalidateSize());
                },
              }}
            />
            <ResizeOnShow />
            <Marker position={[lat, lng]} />
            {radius > 0 && <Circle center={[lat, lng]} radius={radius} />}
          </SafeLeaflet>
        </Box>
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
    const off = bus.on("chat:ask", (q: any) => {
      bus.emit("station:selected", { id: "BTS-001", name: "朝阳-望京2号站", suggest: q });
    });
    return () => off && off();
  }, [bus]);

  return (
    <BusCtx.Provider value={bus}>
      <div className="w-full h-full p-4">
        {/* lg 尺寸以上采用 3:7 网格；想改比例，改 3fr/7fr 即可 */}
        <div className="grid grid-cols-1 lg:grid-cols-[4.5fr_5.5fr] gap-4 h-[calc(100vh-2rem)]">
          {/* ❌ <ChatPane ref={chatRef}/> → ✅ <ChatPane/> */}
          <div className="h-full min-h-0 flex">
            <ChatPane fill />
          </div>
          <div className="h-full min-h-0 overflow-hidden"><ToolPane/></div>
        </div>
      </div>
    </BusCtx.Provider>
  );
}
