import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { useAuth } from "../../auth/AuthProvider";
import * as api from "./api";
import type { NoteCreateInput, NoteFilters } from "./types";

export const notesKeys = {
  all: ["notes"] as const,
  lists: (filters: NoteFilters) => ["notes", "list", { ...filters, tags: [...(filters.tags ?? [])] }] as const,
  detail: (id: string) => ["notes", "detail", id] as const,
  capabilities: ["notes", "capabilities"] as const,
};

export function flattenNotes(data?: { pages: Array<{ items: unknown[] }> }) {
  const seen = new Set<string>();
  return (data?.pages.flatMap((page) => page.items) ?? []).filter((item) => {
    const id = (item as { id?: string }).id;
    if (!id || seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

export function useNotesCapabilities() {
  const { request } = useAuth();
  return useQuery({ queryKey: notesKeys.capabilities, queryFn: ({ signal }) => api.getNoteCapabilities(request, signal), staleTime: 60_000, retry: false });
}

export function useNotesQuery(filters: NoteFilters, enabled = true) {
  const { request } = useAuth();
  const query = useInfiniteQuery({
    queryKey: notesKeys.lists(filters),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => api.listNotes(request, { ...filters, cursor: pageParam }, signal),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    // NOTE: 筛选参数切换时保留上一份列表数据，防止 NotesPage 因 isPending 瞬间切回加载态而卸载工作区和移动端筛选弹层
    placeholderData: keepPreviousData,
    enabled,
    retry: false,
  });
  const items = useMemo(() => flattenNotes(query.data) as ReturnType<typeof flattenNotes>, [query.data]);
  return { ...query, items };
}

export function useNoteQuery(id?: string, enabled = true) {
  const { request } = useAuth();
  return useQuery({ queryKey: notesKeys.detail(id ?? ""), queryFn: ({ signal }) => api.getNote(request, id!, signal), enabled: Boolean(id) && enabled, retry: false });
}

export function useNoteMutations() {
  const { request } = useAuth();
  const client = useQueryClient();
  const invalidateLists = () => void client.invalidateQueries({ queryKey: notesKeys.all });
  const create = useMutation({ mutationFn: (apiInput: NoteCreateInput) => api.createNote(request, apiInput), onSuccess: (note) => { client.setQueryData(notesKeys.detail(note.id), note); invalidateLists(); } });
  const update = useMutation({ mutationFn: ({ id, input }: { id: string; input: Parameters<typeof api.updateNote>[2] }) => api.updateNote(request, id, input), onSuccess: (note) => { client.setQueryData(notesKeys.detail(note.id), note); invalidateLists(); } });
  const remove = useMutation({ mutationFn: ({ id, input }: { id: string; input: Parameters<typeof api.deleteNote>[2] }) => api.deleteNote(request, id, input), onSuccess: (_value, variables) => { client.removeQueries({ queryKey: notesKeys.detail(variables.id), exact: true }); invalidateLists(); } });
  const clear = useMutation({ mutationFn: () => api.clearNotes(request), onSuccess: () => { client.removeQueries({ queryKey: ["notes", "detail"] }); invalidateLists(); } });
  const retryProjection = useMutation({ mutationFn: () => api.retryNoteProjections(request), onSuccess: invalidateLists });
  return { create, update, remove, clear, retryProjection };
}
