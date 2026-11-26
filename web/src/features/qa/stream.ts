import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useAuth } from "../../auth/AuthProvider";
import { qaMessageStreamUrl } from "./api";
import { qaMessagesKey } from "./queries";
import type { QaSource } from "./types";

export const qaStages = ["retrieving", "reranking", "verifying", "generating"] as const;
type QaStage = typeof qaStages[number];

export type QaLiveAnswer = {
  messageId: string;
  stages: QaStage[];
  currentStage?: QaStage;
  retrievedCount?: number;
  content: string;
  sources: QaSource[];
  status: "receiving" | "done" | "disconnected";
};

type ScopedAnswer = QaLiveAnswer & {
  conversationId: string;
  session: string;
};

function parseEvent(event: Event): unknown {
  if (!(event instanceof MessageEvent) || typeof event.data !== "string") return;
  try {
    return JSON.parse(event.data);
  } catch {
    return;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function useQaAnswerStream(conversationId?: string, messageId?: string) {
  const auth = useAuth();
  const session = auth.status === "authenticated" && !auth.isLogoutPending
    ? auth.csrfToken
    : undefined;
  const client = useQueryClient();
  const [answer, setAnswer] = useState<ScopedAnswer>();

  useEffect(() => setAnswer(undefined), [conversationId, session]);

  useEffect(() => {
    if (!session || !conversationId || !messageId || typeof EventSource !== "function") return;

    const source = new EventSource(qaMessageStreamUrl(conversationId, messageId));
    let closed = false;
    setAnswer({
      conversationId, session, messageId,
      stages: [], content: "", sources: [], status: "receiving",
    });

    const update = (change: (current: ScopedAnswer) => ScopedAnswer) => {
      if (closed) return;
      setAnswer((current) => current?.conversationId === conversationId
        && current.session === session && current.messageId === messageId
        ? change(current) : current);
    };

    source.addEventListener("stage", (event) => {
      const payload = parseEvent(event);
      if (!isRecord(payload)) return;
      const stage = qaStages.find((name) => name === payload.stage);
      if (!stage) return;
      const count = payload.retrieved_count;
      update((current) => ({
        ...current,
        stages: current.stages.includes(stage) ? current.stages : [...current.stages, stage],
        currentStage: stage,
        retrievedCount: typeof count === "number" && Number.isSafeInteger(count) && count >= 0
          ? count : current.retrievedCount,
      }));
    });
    source.addEventListener("token", (event) => {
      const payload = parseEvent(event);
      if (!isRecord(payload) || typeof payload.delta !== "string") return;
      const delta = payload.delta;
      update((current) => ({ ...current, content: current.content + delta }));
    });
    source.addEventListener("citations", (event) => {
      const sources = parseEvent(event);
      if (Array.isArray(sources)) update((current) => ({ ...current, sources }));
    });

    const finish = (done: boolean) => {
      if (closed) return;
      update((current) => done
        ? { ...current, status: "done" }
        : { ...current, status: "disconnected", content: "", sources: [] });
      closed = true;
      source.close();
      void client.invalidateQueries({ queryKey: qaMessagesKey(conversationId) });
    };
    source.addEventListener("done", () => finish(true));
    source.addEventListener("error", () => finish(false));

    return () => {
      if (!closed) {
        closed = true;
        source.close();
      }
    };
  }, [client, conversationId, messageId, session]);

  return answer?.conversationId === conversationId && answer?.session === session
    ? answer
    : undefined;
}
