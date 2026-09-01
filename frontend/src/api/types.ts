/**
 * TypeScript mirror of the server's CompanyOverview contract.
 *
 * Mirrors src/marketsentinel/domain.py exactly (field names, optionality, literal unions).
 * This file must not add fields, invent defaults, or re-derive values the server already
 * computed: extraction and materiality stay separate, and the deterministic layers are
 * recomputed server-side, so a client that re-derived them would be a second opinion. See
 * docs/architecture/ARCHITECTURE.md and AGENTS.md "Do not invent consequential decisions".
 */

export type MarketName = "S&P 500" | "FTSE 100";

export interface Constituent {
  symbol: string;
  yahoo_symbol: string;
  name: string;
  market: MarketName;
  aliases: string[];
}

export interface UniverseResult {
  constituents: Constituent[];
  source: string;
  is_fallback: boolean;
  fetched_at: string;
  message: string | null;
}

export type EventType =
  | "earnings"
  | "product_launch"
  | "investment"
  | "acquisition"
  | "regulation"
  | "litigation"
  | "supply_disruption"
  | "management_change"
  | "financing"
  | "macroeconomic_exposure"
  | "partnership"
  | "contract_award"
  | "contract_loss"
  | "analyst_or_guidance_change"
  | "other"
  | "uncertain";

export type EventDirection = "positive" | "negative" | "mixed" | "neutral" | "uncertain";

export type TimeHorizon = "immediate" | "days" | "weeks" | "months" | "long_term" | "uncertain";

export type EvidenceStatus = "corroborated" | "contradicted" | "unsupported" | "uncertain";

export type SourceClass =
  | "official_company"
  | "regulatory_or_filing"
  | "major_financial_news"
  | "industry_specialist"
  | "general_news"
  | "commentary_or_opinion"
  | "unknown";

export type RiskTheme =
  | "export_trade"
  | "regulatory_antitrust"
  | "legal_litigation"
  | "cybersecurity"
  | "supply_constraint"
  | "demand_slowdown"
  | "customer_concentration"
  | "competitive_pressure"
  | "execution_operational"
  | "capital_allocation"
  | "guidance_valuation"
  | "macro_geographic"
  | "key_person_management"
  | "unmapped";

export type RiskBand = "Severe" | "Elevated" | "Moderate" | "Watch";

export interface CompanyReference {
  symbol: string;
  name: string;
}

export interface ArticleEvidenceReference {
  article_id: string;
  title: string;
  publisher: string;
  published_at: string;
  url: string;
}

export interface EventExtraction {
  event_type: EventType;
  summary: string;
  direction: EventDirection;
  magnitude: number;
  time_horizon: TimeHorizon;
  model_confidence: number;
  important_claims: string[];
  uncertainties: string[];
  positive_channels: string[];
  negative_channels: string[];
}

export interface ClaimAssessment {
  claim_id: string;
  status: EvidenceStatus;
  reasoning: string;
  evidence_article_ids: string[];
  confidence: number;
}

export interface RelatedCompanyAnalysis {
  ticker: string;
  relationship_context: string;
  possible_effect_direction: EventDirection;
  reasoning: string;
  confidence: number;
  company: CompanyReference;
}

export interface CompanyIntelligenceEvent {
  article_id: string;
  source_reference: ArticleEvidenceReference;
  source_class: SourceClass;
  subject_company: CompanyReference;
  event: EventExtraction;
  claims: ClaimAssessment[];
  related_companies: RelatedCompanyAnalysis[];
  evidence_strength: number;
  evidence_sources: ArticleEvidenceReference[];
}

export interface CorroborationView {
  total_claims: number;
  corroborated_claims: number;
  contradicted_claims: number;
  unresolved_claims: number;
  comparison_articles: number;
  supporting_articles: number;
  external_sources: number;
  primary_is_official: boolean;
  metric_label: string;
  summary_label: string;
  contradiction_label: string | null;
  breakdown_label: string;
}

export interface KeyDevelopmentView {
  article_id: string;
  event: CompanyIntelligenceEvent;
  members: CompanyIntelligenceEvent[];
  publisher_count: number;
  impact_label: string;
  impact_score: number;
  tier_label: string;
  primary_source_label: string;
  provenance_note: string;
  corroboration: CorroborationView;
}

export interface MaterialityDiagnosticsView {
  considered: number;
  material: number;
  developments: number;
  rendered: number;
  rejected: number;
  rejected_by_condition: Record<string, number>;
}

export interface KeyDevelopmentsView {
  rows: KeyDevelopmentView[];
  diagnostics: MaterialityDiagnosticsView;
  caption: string;
  empty_message: string;
}

export interface TodaysIntelligenceCardView {
  article_id: string;
  event: CompanyIntelligenceEvent;
  impact_label: string;
  impact_score: number;
  primary_source_label: string;
  corroboration: CorroborationView;
}

export interface TodaysIntelligenceView {
  cards: TodaysIntelligenceCardView[];
  caption: string;
  empty_message: string;
}

export interface RankedRisk {
  theme: RiskTheme;
  concern_index: number;
  band: RiskBand;
  summary: string;
  primary_article_id: string;
  primary_article_url: string;
  primary_publisher: string;
  first_evidenced_at: string;
  latest_published_at: string;
  supporting_article_ids: string[];
  supporting_publishers: string[];
  supporting_signal_count: number;
  supporting_event_group_count: number;
}

export interface RiskRowView {
  rank: number;
  label: string;
  concern_index: number;
  band: RiskBand;
  band_color: string;
  summary: string;
  risk: RankedRisk;
}

export interface RiskDiagnostics {
  considered_analyses: number;
  eligible_analyses: number;
  signals_extracted: number;
  realized_signals: number;
  prospective_signals: number;
  unmapped_signals: number;
  themes_ranked: number;
  event_groups: number;
  severe_band_capped: number;
}

export interface TopRisksView {
  rows: RiskRowView[];
  diagnostics: RiskDiagnostics;
  caption: string;
  empty_message: string;
}

export interface MarketViewSummaryView {
  price_note: string;
  sentiment_note: string;
  risk_note: string;
  intelligence_note: string;
}

export interface PricePoint {
  date: string;
  close: number;
  volume: number;
}

export interface DailySentiment {
  ticker: string;
  date: string;
  score: number;
  moving_average_7d: number;
  trend_3: number;
  article_count: number;
  positive_share: number;
  negative_share: number;
  weighted_disagreement: number;
  aggregate_weight: number;
  computed_at: string;
}

export interface AnalyzedEvent {
  article_id: string;
  event_date: string;
  article_url: string;
  event_type: EventType;
  summary: string;
  direction: EventDirection;
  magnitude: number;
  extraction_confidence: number;
  evidence_strength: number;
}

export interface ChartTimeframeView {
  timeframe: string;
  start_date: string | null;
  end_date: string | null;
  price_observations: number;
  sentiment_observations: number;
  markers: AnalyzedEvent[];
  sentiment_coverage_note: string | null;
}

export type ChartStatus = "available" | "unavailable";

export interface ChartView {
  status: ChartStatus;
  message: string | null;
  source: string | null;
  fetched_at: string | null;
  points: PricePoint[];
  daily_sentiment: DailySentiment[];
  default_timeframe: string;
  timeframes: ChartTimeframeView[];
}

export interface CoverageView {
  articles: number;
  analysed_articles: number;
  window_days: number;
  latest_sentiment: DailySentiment | null;
}

export interface ArticleRowView {
  article_id: string;
  title: string;
  url: string;
  source: string;
  published_at: string;
  label: "positive" | "negative" | "neutral";
  sentiment_score: number;
  positive: number;
  negative: number;
  neutral: number;
  is_demo: boolean;
  has_compatible_analysis: boolean;
}

export interface RelevantNewsView {
  articles: ArticleRowView[];
  window_days: number;
  caption: string;
  empty_message: string;
}

/**
 * One stored analysis read directly by article. Structurally the same as
 * TodaysIntelligenceCardView because it is the same record described by the same server-owned
 * labels — the detail renderer accepts either.
 */
export interface StoredArticleAnalysisView {
  article_id: string;
  event: CompanyIntelligenceEvent;
  impact_label: string;
  impact_score: number;
  primary_source_label: string;
  corroboration: CorroborationView;
}

/**
 * Deployment scope, advisory only — the POST restrictions are enforced server-side regardless.
 * `prepared_companies` labels the deliberately backfilled deep demos; `coverage` is the raw
 * stored-article count per ticker. Neither restricts search or reads: the full constituent
 * universe stays searchable in both modes.
 */
export interface CapabilitiesView {
  mode: "public" | "private";
  default_symbol: string;
  prepared_companies: string[];
  coverage: Record<string, number>;
  supports_refresh: boolean;
  supports_article_analysis: boolean;
}

export type DataSource = "stored" | "refreshed";

export interface CompanyOverview {
  constituent: Constituent;
  data_source: DataSource;
  generated_at: string;
  coverage: CoverageView;
  market_view: MarketViewSummaryView;
  chart: ChartView;
  key_developments: KeyDevelopmentsView;
  todays_intelligence: TodaysIntelligenceView;
  top_risks: TopRisksView;
  disclaimer: string;
}
