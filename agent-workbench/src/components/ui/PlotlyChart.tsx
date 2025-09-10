"use client";
import dynamic from "next/dynamic";
import { Layout, Config, Data } from "plotly.js";

import React from "react";



const Plot = dynamic(() => import("react-plotly.js"), { ssr: false });

// 统一的“轻量主题”
const DEFAULT_LAYOUT: Partial<Plotly.Layout> = {
  template: "simple_white" as unknown as Layout["template"],
  // 或者
  // template: "simple_white" as unknown as Plotly.Template,  // 方式二：断言为 Template
  paper_bgcolor: "rgba(0,0,0,0)",
  plot_bgcolor: "rgba(0,0,0,0)",
  font: {
    family: "Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
    size: 12,
    color: "#111827",
  },
  margin: { l: 40, r: 20, t: 40, b: 40 },
  colorway: ["#4f46e5", "#06b6d4", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#14b8a6"],
  hoverlabel: {
    bgcolor: "rgba(255,255,255,0.9)",
    bordercolor: "#e5e7eb",
    font: { color: "#111827" },
  },
};


const DEFAULT_CONFIG: Partial<Plotly.Config> = {
  displayModeBar: false,
  responsive: true,
};

type PlotlyChartProps = {
  data?: Data[];
  layout?: Partial<Layout>;
  config?: Partial<Config>;
};

export default function PlotlyChart({ data = [], layout = {}, config = {} }: PlotlyChartProps) {
  const mergedLayout: Partial<Layout> = { ...DEFAULT_LAYOUT, ...layout };
  const mergedConfig: Partial<Config> = { ...DEFAULT_CONFIG, ...config };

  return (
    <Plot
      data={data}
      layout={mergedLayout}
      config={mergedConfig}
      useResizeHandler
      className="w-full h-full"
      style={{ width: "100%", height: "100%" }}
    />
  );
}
