"use client";
import dynamic from "next/dynamic";
import React from "react";
import type { Layout, Config, Data, PlotlyHTMLElement } from "plotly.js";

const Plot = dynamic(() => import("react-plotly.js"), { ssr: false });

const DEFAULT_LAYOUT: Partial<Layout> = {
  template: "simple_white" as any,
  paper_bgcolor: "rgba(0,0,0,0)",
  plot_bgcolor: "rgba(0,0,0,0)",
  font: {
    family: "Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
    size: 12,
    color: "#111827",
  },
  margin: { l: 40, r: 20, t: 40, b: 40 },
  colorway: [
    "#4f46e5",
    "#06b6d4",
    "#10b981",
    "#f59e0b",
    "#ef4444",
    "#8b5cf6",
    "#14b8a6",
  ],
  hoverlabel: {
    bgcolor: "rgba(255,255,255,0.9)",
    bordercolor: "#e5e7eb",
    font: { color: "#111827" },
  },
};

const DEFAULT_CONFIG: Partial<Config> = {
  displayModeBar: false,
  responsive: true,
};

type PlotlyChartProps = {
  data?: Data[];
  layout?: Partial<Layout>;
  config?: Partial<Config>;
  onReady?: (div: PlotlyHTMLElement) => void; // ✅ 新增
};

export default function PlotlyChart({
  data = [],
  layout = {},
  config = {},
  onReady,
}: PlotlyChartProps) {
  const mergedLayout = React.useMemo(
    () => ({ ...DEFAULT_LAYOUT, ...layout }),
    [layout]
  );
  const mergedConfig = React.useMemo(
    () => ({ ...DEFAULT_CONFIG, ...config }),
    [config]
  );
  const divRef = React.useRef<PlotlyHTMLElement | null>(null);

  return (
    <Plot
      data={data}
      layout={mergedLayout}
      config={mergedConfig}
      useResizeHandler
      className="w-full h-full"
      style={{ width: "100%", height: "100%" }}
      onInitialized={(_, gd) => {
        divRef.current = gd as unknown as PlotlyHTMLElement;
        onReady?.(divRef.current);
      }}
      onUpdate={(_, gd) => {
        divRef.current = gd as unknown as PlotlyHTMLElement;
        onReady?.(divRef.current);
      }}
    />
  );
}
