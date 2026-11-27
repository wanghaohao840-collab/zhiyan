import { useCallback, useEffect, useRef, useState } from 'react';
import {
  App,
  applyDocumentTheme,
  applyHostStyleVariables,
} from '@modelcontextprotocol/ext-apps';
import { z } from 'zod';
import { toFileUri } from '../utils/format';

export interface InitialToolPayload {
  view?: 'monitor' | 'packets' | 'dispatch';
  selected_session_id?: string | null;
  snapshot?: unknown;
  run_id?: string;
  session_id?: string;
}

export interface UseMcpAppResult {
  connected: boolean;
  hostTheme: 'light' | 'dark';
  canSendMessage: boolean;
  initialPayload: InitialToolPayload | null;
  connectionError: string | null;
  callServerTool: <T = Record<string, unknown>>(
    name: string,
    args?: Record<string, unknown>,
  ) => Promise<T>;
  openWorkspaceFile: (absolutePath: string) => Promise<boolean>;
  sendMessageToChat: (text: string) => Promise<boolean>;
}

const toggleAuxResponseSchema = z
  .object({
    isError: z.boolean().optional(),
  })
  .passthrough();

/**
 * 封装 @modelcontextprotocol/ext-apps 生命周期、宿主主题变量同步、
 * 服务端 app-only 工具调用及 Antigravity 侧边栏文件打开扩展。
 */
export function useMcpApp(): UseMcpAppResult {
  const appRef = useRef<App | null>(null);
  const [connected, setConnected] = useState(false);
  const [hostTheme, setHostTheme] = useState<'light' | 'dark'>('light');
  const [canSendMessage, setCanSendMessage] = useState(false);
  const [initialPayload, setInitialPayload] = useState<InitialToolPayload | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);

  useEffect(() => {
    const app = new App({ name: 'Codex Monitor', version: '1.0.0' }, {});
    appRef.current = app;

    // NOTE: 严格遵循 mcp-app-development 规范，先注册所有回调再调用 connect()
    app.ontoolinput = (params) => {
      const args = (params?.arguments ?? {}) as Record<string, unknown>;
      if (typeof args.session_id === 'string' || typeof args.view === 'string') {
        setInitialPayload((prev) => ({
          ...prev,
          view: (args.view as InitialToolPayload['view']) ?? prev?.view,
          selected_session_id:
            typeof args.session_id === 'string' ? args.session_id : prev?.selected_session_id,
        }));
      }
    };

    app.ontoolresult = (result) => {
      const sc = (result as { structuredContent?: InitialToolPayload })?.structuredContent;
      if (sc && typeof sc === 'object') {
        setInitialPayload(sc);
      }
    };

    app.onhostcontextchanged = (ctx) => {
      if (ctx?.theme === 'dark' || ctx?.theme === 'light') {
        applyDocumentTheme(ctx.theme);
        setHostTheme(ctx.theme);
      }
      if (ctx?.styles?.variables) {
        applyHostStyleVariables(ctx.styles.variables);
      }
    };

    let active = true;
    app
      .connect()
      .then(() => {
        if (!active) return;
        const ctx = app.getHostContext();
        if (ctx?.theme === 'dark' || ctx?.theme === 'light') {
          applyDocumentTheme(ctx.theme);
          setHostTheme(ctx.theme);
        }
        if (ctx?.styles?.variables) {
          applyHostStyleVariables(ctx.styles.variables);
        }
        const caps = app.getHostCapabilities() as Record<string, unknown> | undefined;
        setCanSendMessage(Boolean(caps && caps.message));
        setConnected(true);
      })
      .catch((err: unknown) => {
        if (!active) return;
        const message = err instanceof Error ? err.message : String(err);
        setConnectionError(message);
      });

    return () => {
      active = false;
    };
  }, []);

  const callServerTool = useCallback(
    async <T = Record<string, unknown>>(
      name: string,
      args: Record<string, unknown> = {},
    ): Promise<T> => {
      const app = appRef.current;
      if (!app) {
        throw new Error('MCP App 尚未初始化');
      }
      const res = await app.callServerTool({ name, arguments: args });
      const typed = res as {
        isError?: boolean;
        structuredContent?: T & { error?: string };
        content?: Array<{ type: string; text?: string }>;
      };
      if (typed.isError) {
        const errText =
          typed.structuredContent?.error ||
          typed.content?.map((c) => c.text).filter(Boolean).join('\n') ||
          `调用工具 ${name} 失败`;
        throw new Error(errText);
      }
      return (typed.structuredContent ?? {}) as T;
    },
    [],
  );

  const openWorkspaceFile = useCallback(async (absolutePath: string): Promise<boolean> => {
    const app = appRef.current;
    if (!app || !absolutePath) return false;
    try {
      const resp = await app.request(
        {
          method: 'com.google.antigravity/toggle-aux-pane',
          params: {
            tabType: 'fileView',
            uri: toFileUri(absolutePath),
          },
        },
        toggleAuxResponseSchema,
      );
      return !resp.isError;
    } catch {
      return false;
    }
  }, []);

  const sendMessageToChat = useCallback(async (text: string): Promise<boolean> => {
    const app = appRef.current;
    if (!app || !canSendMessage) return false;
    try {
      await app.sendMessage({
        role: 'user',
        content: [{ type: 'text', text }],
      });
      return true;
    } catch {
      return false;
    }
  }, [canSendMessage]);

  return {
    connected,
    hostTheme,
    canSendMessage,
    initialPayload,
    connectionError,
    callServerTool,
    openWorkspaceFile,
    sendMessageToChat,
  };
}
