import type { AuthContextValue } from "../../auth/AuthProvider";
import type {
  QaActiveJob, QaCapabilities, QaConversation, QaConversationPage, QaDeletion, QaJob,
  QaMessage, QaMessagePage, QaMode,
} from "./types";

type AuthRequest = AuthContextValue["request"];
const json = (body: unknown) => ({
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
const segment = (value: string) => encodeURIComponent(value);
const pageQuery = (limit: number, cursor?: string | null) => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) params.set("cursor", cursor);
  return params.toString();
};

export const getQaCapabilities = (request: AuthRequest, signal?: AbortSignal) =>
  request<QaCapabilities>("/api/v1/qa/capabilities", { signal });
export const listQaConversations = (request: AuthRequest, cursor?: string | null, signal?: AbortSignal) =>
  request<QaConversationPage>(`/api/v1/qa/conversations?${pageQuery(20, cursor)}`, { signal });
export const getQaConversation = (request: AuthRequest, id: string, signal?: AbortSignal) =>
  request<QaConversation>(`/api/v1/qa/conversations/${segment(id)}`, { signal });
export const listQaMessages = (request: AuthRequest, id: string, cursor?: string | null, signal?: AbortSignal) =>
  request<QaMessagePage>(`/api/v1/qa/conversations/${segment(id)}/messages?${pageQuery(50, cursor)}`, { signal });
export const getQaActiveSummary = (request: AuthRequest, id: string, signal?: AbortSignal) =>
  request<QaActiveJob>(`/api/v1/qa/conversations/${segment(id)}/summary-jobs/active`, { signal });
export const createQaConversation = (request: AuthRequest, documentIds: string[]) =>
  request<QaConversation>("/api/v1/qa/conversations", { method: "POST", ...json({ document_ids: documentIds }) });
export const askQaQuestion = (request: AuthRequest, id: string, question: string, mode: QaMode, clientRequestId: string, stream = false) =>
  request<QaMessage>(`/api/v1/qa/conversations/${segment(id)}/messages${stream ? "?stream=true" : ""}`, { method: "POST", ...json({ question, mode, client_request_id: clientRequestId }) });
export const qaMessageStreamUrl = (conversationId: string, messageId: string) =>
  `/api/v1/qa/conversations/${segment(conversationId)}/messages/${segment(messageId)}/stream`;
export const getQaMessage = (request: AuthRequest, id: string, signal?: AbortSignal) =>
  request<QaMessage>(`/api/v1/qa/messages/${segment(id)}`, { signal });
export const retryQaMessage = (
  request: AuthRequest,
  id: string,
  clientRequestId: string,
  stream = false,
) =>
  request<QaMessage>(
    `/api/v1/qa/messages/${segment(id)}/retry${stream ? "?stream=true" : ""}`,
    { method: "POST", ...json({ client_request_id: clientRequestId }) },
  );
export const startQaSummary = (request: AuthRequest, id: string, instruction: string, clientRequestId: string) =>
  request<QaJob>(`/api/v1/qa/conversations/${segment(id)}/summary-jobs`, { method: "POST", ...json({ instruction: instruction || null, client_request_id: clientRequestId }) });
export const getQaJob = (request: AuthRequest, id: string, signal?: AbortSignal) =>
  request<QaJob>(`/api/v1/qa/jobs/${segment(id)}`, { signal });
export const cancelQaJob = (request: AuthRequest, id: string) =>
  request<QaJob>(`/api/v1/qa/jobs/${segment(id)}/cancel`, { method: "POST" });
export const deleteQaConversation = (request: AuthRequest, id: string) =>
  request<QaDeletion>(`/api/v1/qa/conversations/${segment(id)}`, { method: "DELETE" });
export const getQaDeletion = (request: AuthRequest, id: string, signal?: AbortSignal) =>
  request<QaDeletion>(`/api/v1/qa/deletions/${segment(id)}`, { signal });
