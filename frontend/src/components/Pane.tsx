import type { ReactNode } from "react";

interface PaneProps {
  title: string;
  meta?: string;
  controls?: ReactNode;
  className?: string;
  children: ReactNode;
}

/** The system's base container: paper, a 32px header with one hairline rule, no card, no shadow. */
export function Pane({ title, meta, controls, className, children }: PaneProps) {
  return (
    <section className={`ms-pane${className ? ` ${className}` : ""}`}>
      <header className="ms-pane-header">
        <h2 className="ms-pane-title">{title}</h2>
        {meta && <span className="ms-pane-meta">{meta}</span>}
        {controls && <div className="ms-pane-controls">{controls}</div>}
      </header>
      <div className="ms-pane-body">{children}</div>
    </section>
  );
}
