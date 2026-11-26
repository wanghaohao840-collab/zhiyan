import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { InfiniteData } from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";
import { ApiError } from "../../api/client";
import { useAuth } from "../../auth/AuthProvider";
import { DOCUMENTS_QUERY_KEY } from "../documents/queries";
import * as api from "./api";
import type { QaConversation, QaConversationPage, QaDeletion, QaJob, QaMessage, QaMessagePage, QaMode } from "./types";

export const QA_CAPABILITIES_KEY = ["qa", "capabilities"] as const;
export const QA_CONVERSATIONS_KEY = ["qa", "conversations"] as const;
export const qaConversationKey = (id: string) => ["qa", "conversation", id] as const;
export const qaMessagesKey = (id: string) => ["qa", "messages", id] as const;
export const qaJobKey = (id: string) => ["qa", "job", id] as const;
export const qaDeletionKey = (id: string) => ["qa", "deletion", id] as const;
export const qaActiveSummaryKey = (id: string) => ["qa", "active-summary", id] as const;
const activeJob = (job?: QaJob) => job?.status === "queued" || job?.status === "running";
const activeDeletion = (item?: QaDeletion) => item?.status === "queued" || item?.status === "running";

function uniqueById<T>(items: T[], id: (item: T) => string): T[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const value = id(item);
    if (seen.has(value)) return false;
    seen.add(value);
    return true;
  });
}

export function flattenQaConversations(
  data?: InfiniteData<QaConversationPage>,
): QaConversation[] {
  return uniqueById(
    data?.pages.flatMap((page) => page.items) ?? [],
    (item) => item.conversation_id,
  );
}

export function flattenQaMessages(
  data?: InfiniteData<QaMessagePage>,
): QaMessage[] {
  const newestCopies = new Map<string, QaMessage>();
  for (const page of data?.pages ?? []) {
    for (const item of page.items) {
      if (!newestCopies.has(item.message_id)) {
        newestCopies.set(item.message_id, item);
      }
    }
  }
  return uniqueById(
    [...(data?.pages ?? [])].reverse().flatMap((page) => page.items),
    (item) => item.message_id,
  ).map((item) => newestCopies.get(item.message_id)!);
}

export function useQaCapabilities() {
  const { request } = useAuth();
  return useQuery({ queryKey: QA_CAPABILITIES_KEY, queryFn: ({ signal }) => api.getQaCapabilities(request, signal), staleTime: 60_000 });
}
export function useQaConversations(enabled = true) {
  const { request } = useAuth();
  const query = useInfiniteQuery({
    queryKey: QA_CONVERSATIONS_KEY,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => api.listQaConversations(request, pageParam, signal),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled,
  });
  const items = useMemo(() => flattenQaConversations(query.data), [query.data]);
  const data = useMemo(
    () => query.data ? { ...query.data, items } : undefined,
    [items, query.data],
  );
  return { ...query, data, items };
}
export function useQaConversation(id?: string) {
  const { request } = useAuth();
  return useQuery({ queryKey: qaConversationKey(id ?? ""), queryFn: ({ signal }) => api.getQaConversation(request, id!, signal), enabled: Boolean(id) });
}
export function useQaMessages(id?: string) {
  const { request } = useAuth();
  const query = useInfiniteQuery({
    queryKey: qaMessagesKey(id ?? ""),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => api.listQaMessages(request, id!, pageParam, signal),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: Boolean(id),
    refetchInterval: (current) => current.state.data?.pages
      .some((page) => page.items.some((item) => item.status === "pending"))
      ? 1500
      : false,
  });
  const items = useMemo(() => flattenQaMessages(query.data), [query.data]);
  const data = useMemo(
    () => query.data ? { ...query.data, items } : undefined,
    [items, query.data],
  );
  return { ...query, data, items };
}
export function useQaActiveSummary(id?: string) {
  const { request } = useAuth();
  return useQuery({
    queryKey: qaActiveSummaryKey(id ?? ""),
    queryFn: ({ signal }) => api.getQaActiveSummary(request, id!, signal),
    enabled: Boolean(id),
  });
}
export function useQaJob(id?: string) {
  const { request } = useAuth();
  const client = useQueryClient();
  const reconciled = useRef<string | undefined>(undefined);
  const query = useQuery({ queryKey: qaJobKey(id ?? ""), queryFn: ({ signal }) => api.getQaJob(request, id!, signal), enabled: Boolean(id), refetchInterval: (current) => activeJob(current.state.data) ? 1500 : false });
  useEffect(() => {
    const job = query.data;
    if (job && !activeJob(job) && reconciled.current !== job.updated_at) {
      reconciled.current = job.updated_at;
      void client.invalidateQueries({ queryKey: qaConversationKey(job.conversation_id) });
      void client.invalidateQueries({ queryKey: qaMessagesKey(job.conversation_id) });
      void client.invalidateQueries({ queryKey: qaActiveSummaryKey(job.conversation_id) });
    }
  }, [client, query.data]);
  return query;
}
export function useQaDeletion(id?: string) {
  const { request } = useAuth();
  const client = useQueryClient();
  const reconciled = useRef<string | undefined>(undefined);
  const query = useQuery({ queryKey: qaDeletionKey(id ?? ""), queryFn: ({ signal }) => api.getQaDeletion(request, id!, signal), enabled: Boolean(id), refetchInterval: (current) => activeDeletion(current.state.data) ? 1500 : false });
  useEffect(() => {
    const item = query.data;
    if (item && !activeDeletion(item) && reconciled.current !== item.updated_at) {
      reconciled.current = item.updated_at;
      void client.invalidateQueries({ queryKey: QA_CONVERSATIONS_KEY });
      void client.invalidateQueries({ queryKey: DOCUMENTS_QUERY_KEY });
    }
  }, [client, query.data]);
  return query;
}
export function useQaMutations() {
  const { request } = useAuth();
  const client = useQueryClient();
  const refresh = (conversationId?: string) => {
    void client.invalidateQueries({ queryKey: QA_CONVERSATIONS_KEY });
    if (conversationId) {
      void client.invalidateQueries({ queryKey: qaConversationKey(conversationId) });
      void client.invalidateQueries({ queryKey: qaMessagesKey(conversationId) });
    }
  };
  const create = useMutation({ mutationFn: (documentIds: string[]) => api.createQaConversation(request, documentIds), onSuccess: (item) => refresh(item.conversation_id) });
  const retryableOnce = (failureCount: number, error: Error) => failureCount < 1 && error instanceof ApiError && error.retryable;
  const ask = useMutation({
    mutationFn: (v: { conversationId: string; question: string; mode: QaMode; clientRequestId: string; stream?: boolean }) =>
      api.askQaQuestion(request, v.conversationId, v.question, v.mode, v.clientRequestId, v.stream),
    retry: retryableOnce,
    onSuccess: (item) => refresh(item.conversation_id),
  });
  const retry = useMutation({ mutationFn: (v: { messageId: string; conversationId: string; clientRequestId: string }) => api.retryQaMessage(request, v.messageId, v.clientRequestId), retry: retryableOnce, onSuccess: (item) => refresh(item.conversation_id) });
  const summarize = useMutation({ mutationFn: (v: { conversationId: string; instruction: string; clientRequestId: string }) => api.startQaSummary(request, v.conversationId, v.instruction, v.clientRequestId), retry: retryableOnce, onSuccess: (job) => { client.setQueryData(qaJobKey(job.job_id), job); client.setQueryData(qaActiveSummaryKey(job.conversation_id), { job }); } });
  const cancel = useMutation({ mutationFn: (jobId: string) => api.cancelQaJob(request, jobId), onSuccess: (job) => client.setQueryData(qaJobKey(job.job_id), job) });
  const remove = useMutation({ mutationFn: (conversationId: string) => api.deleteQaConversation(request, conversationId), onSuccess: (item) => { client.setQueryData(qaDeletionKey(item.deletion_id), item); refresh(); void client.invalidateQueries({ queryKey: DOCUMENTS_QUERY_KEY }); } });
  return { create, ask, retry, summarize, cancel, remove };
}

export function newClientRequestId(): string {
  return crypto.randomUUID();
}
