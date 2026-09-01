import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Desktop rail widths, driven by dragging the vertical dividers.
 *
 * The widths are applied as the same `--rail-w` / `--detail-w` custom properties the stylesheet
 * already uses, overridden on the shell element — so the centre workspace reflows through the
 * existing flex rules with no layout fork, and the mobile stacked layout (which overrides both
 * widths to 100% in its own media queries) is untouched.
 */
export const RAIL_DEFAULT = 196;
export const RAIL_MIN = 150;
export const RAIL_MAX = 380;

export const DETAIL_DEFAULT = 392;
export const DETAIL_MIN = 280;
export const DETAIL_MAX = 620;

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export interface ResizableRails {
  railWidth: number;
  detailWidth: number;
  /** Pointer-down handlers for the two dividers. */
  startRailDrag: (event: React.PointerEvent) => void;
  startDetailDrag: (event: React.PointerEvent) => void;
  /** Double-click a divider to restore its default width. */
  resetRail: () => void;
  resetDetail: () => void;
  isDragging: boolean;
}

export function useResizableRails(): ResizableRails {
  const [railWidth, setRailWidth] = useState(RAIL_DEFAULT);
  const [detailWidth, setDetailWidth] = useState(DETAIL_DEFAULT);
  const [isDragging, setIsDragging] = useState(false);
  const dragRef = useRef<"rail" | "detail" | null>(null);

  useEffect(() => {
    if (!isDragging) return;

    const onMove = (event: PointerEvent) => {
      if (dragRef.current === "rail") {
        // The left rail starts at the viewport edge, so its width is simply the pointer x.
        setRailWidth(clamp(event.clientX, RAIL_MIN, RAIL_MAX));
      } else if (dragRef.current === "detail") {
        // The right rail is anchored to the right edge, so it grows as the pointer moves left.
        setDetailWidth(clamp(window.innerWidth - event.clientX, DETAIL_MIN, DETAIL_MAX));
      }
    };
    const onUp = () => {
      dragRef.current = null;
      setIsDragging(false);
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [isDragging]);

  const start = useCallback((which: "rail" | "detail") => {
    return (event: React.PointerEvent) => {
      event.preventDefault();
      dragRef.current = which;
      setIsDragging(true);
    };
  }, []);

  return {
    railWidth,
    detailWidth,
    startRailDrag: start("rail"),
    startDetailDrag: start("detail"),
    resetRail: useCallback(() => setRailWidth(RAIL_DEFAULT), []),
    resetDetail: useCallback(() => setDetailWidth(DETAIL_DEFAULT), []),
    isDragging,
  };
}
