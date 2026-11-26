export type ChunkLocator = {
  document_id: string;
  chunk_id: string;
  chunk_index: number;
  content_sha256: string;
  page_number?: number | null;
};

export type SearchInput = { query: string; document_ids: string[]; limit: 5 | 10 | 20 };
export type SearchResult = {
  document_id: string;
  document_name: string;
  excerpt: string;
  rank: number;
  score: number | null;
  page_number: number | null;
  section: string | null;
  locator: ChunkLocator;
};
export type SearchResponse = {
  request_id: string;
  document_ids: string[];
  result_count: number;
  results: SearchResult[];
};
