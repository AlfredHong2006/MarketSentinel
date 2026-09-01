/**
 * The draggable divider between a rail and the centre workspace.
 *
 * It sits on the existing 1px seam rather than adding width of its own, so resizing changes only
 * the rail width and never the resting layout. Hidden below the desktop breakpoint, where the
 * rails stack instead of sitting side by side.
 */
export function RailResizer({
  side,
  onPointerDown,
  onReset,
  label,
}: {
  side: "left" | "right";
  onPointerDown: (event: React.PointerEvent) => void;
  onReset: () => void;
  label: string;
}) {
  return (
    <div
      className={`ms-resizer ms-resizer-${side}`}
      role="separator"
      aria-orientation="vertical"
      aria-label={`${label} — drag to resize, double-click to reset`}
      title="Drag to resize · double-click to reset"
      onPointerDown={onPointerDown}
      onDoubleClick={onReset}
    />
  );
}
