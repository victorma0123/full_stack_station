"use client";
import dynamic from "next/dynamic";
import React from "react";

const Plot = dynamic(() => import("react-plotly.js"), { ssr: false });

// 统一的“轻量主题”
const DEFAULT_LAYOUT: Partial<Plotly.Layout> = {
  template: "simple_white",                         // 干净底板
  paper_bgcolor: "rgba(0,0,0,0)",
  plot_bgcolor: "rgba(0,0,0,0)",
  font: { family: "Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial", size: 12, color: "#111827" },
  margin: { l: 40, r: 20, t: 40, b: 40 },
  colorway: ["#4f46e5", "#06b6d4", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#14b8a6"], // 柔和但有识别度
  hoverlabel: { bgcolor: "rgba(255,255,255,0.9)", bordercolor: "#e5e7eb", font: { color: "#111827" } },
};

const DEFAULT_CONFIG: Partial<Plotly.Config> = {
  displayModeBar: false,
  responsive: true,
};

export default function PlotlyChart(props: any) {
  const data   = props.data   ?? [];
  const layout = { ...DEFAULT_LAYOUT, ...(props.layout ?? {}) };
  const config = { ...DEFAULT_CONFIG, ...(props.config ?? {}) };
  return (
    <Plot
      data={data}
      layout={layout as any}
      config={config as any}
      useResizeHandler
      className="w-full h-full"
      style={{ width: "100%", height: "100%" }}
    />
  );
}
