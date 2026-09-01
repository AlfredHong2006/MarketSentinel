export type Selection =
  | { kind: "development"; articleId: string }
  | { kind: "intelligence"; articleId: string }
  | { kind: "risk"; theme: string }
  /** A Relevant News row whose stored analysis is fetched lazily and shown in the detail pane. */
  | { kind: "article"; articleId: string };
