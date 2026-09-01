import { useEffect, useRef, useState } from "react";

/**
 * The measured content box of an element.
 *
 * Recharts' own `ResponsiveContainer` relies solely on `ResizeObserver`. That is fine in a real
 * browser but leaves the chart unable to re-measure in any environment where the observer does
 * not fire — headless Chromium/Edge among them, where a container can demonstrably change width
 * with no observer callback at all. Since a stuck chart width means an SVG wider than its box,
 * i.e. a horizontally clipped chart, this measures through three independent paths instead:
 *
 *  1. `ResizeObserver`, the precise one, when the environment supports it;
 *  2. `window.resize`, which covers viewport changes even without an observer;
 *  3. `resizeKey`, changed by the caller whenever it alters the layout itself — the rail drag.
 *
 * Any one of them is sufficient, so no single missing capability can strand the chart.
 */
export function useElementSize<T extends HTMLElement>(resizeKey?: unknown) {
  const ref = useRef<T | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    const element = ref.current;
    if (!element) return;

    const measure = () => {
      const box = element.getBoundingClientRect();
      const next = { width: Math.round(box.width), height: Math.round(box.height) };
      // Only re-render on a real change; the observer can fire with an identical box.
      setSize((current) =>
        current.width === next.width && current.height === next.height ? current : next,
      );
    };

    measure();

    let observer: ResizeObserver | undefined;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(measure);
      observer.observe(element);
    }
    window.addEventListener("resize", measure);

    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
    // resizeKey re-runs the effect, which re-measures: the caller uses it to report a layout
    // change it made itself, such as dragging a rail divider.
  }, [resizeKey]);

  return [ref, size] as const;
}
