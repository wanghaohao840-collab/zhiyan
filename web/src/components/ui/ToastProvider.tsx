import { CheckCircle2, Info, X, AlertCircle } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

export type ToastTone = "info" | "success" | "danger";

export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface ToastInput {
  message: string;
  tone?: ToastTone;
  durationMs?: number;
  action?: ToastAction;
}

interface ToastItem extends ToastInput {
  id: number;
  tone: ToastTone;
}

interface ToastContextValue {
  push: (input: ToastInput) => number;
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);
const MAX_VISIBLE_TOASTS = 4;

/**
 * 统一非阻塞 Toast 堆叠容器（Sonner 范式）：右下角同方向进出，支持快捷撤销动作与自动定时清理。
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextIdRef = useRef(1);
  const timersRef = useRef<Map<number, number>>(new Map());

  const dismiss = useCallback((id: number) => {
    const timer = timersRef.current.get(id);
    if (timer) {
      window.clearTimeout(timer);
      timersRef.current.delete(id);
    }
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);

  const push = useCallback(
    (input: ToastInput) => {
      const id = nextIdRef.current++;
      const duration = input.durationMs ?? (input.action ? 5000 : 3200);
      const nextItem: ToastItem = {
        ...input,
        id,
        tone: input.tone ?? "info",
      };
      setItems((current) => [...current.slice(-(MAX_VISIBLE_TOASTS - 1)), nextItem]);
      if (duration > 0) {
        const timer = window.setTimeout(() => dismiss(id), duration);
        timersRef.current.set(id, timer);
      }
      return id;
    },
    [dismiss],
  );

  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      timers.forEach((timer) => window.clearTimeout(timer));
      timers.clear();
    };
  }, []);

  const value = useMemo(() => ({ push, dismiss }), [push, dismiss]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      {items.length ? (
        <ol className="ui-toast-stack" aria-label="系统提示" aria-live="polite">
          {items.map((item) => (
            <li key={item.id} className={`ui-toast ui-toast--${item.tone}`} role="status">
              <span className="ui-toast__icon" aria-hidden="true">
                {item.tone === "success" ? (
                  <CheckCircle2 size={16} strokeWidth={1.6} />
                ) : item.tone === "danger" ? (
                  <AlertCircle size={16} strokeWidth={1.6} />
                ) : (
                  <Info size={16} strokeWidth={1.6} />
                )}
              </span>
              <span className="ui-toast__message">{item.message}</span>
              {item.action ? (
                <button
                  type="button"
                  className="ui-toast__action"
                  onClick={() => {
                    item.action?.onClick();
                    dismiss(item.id);
                  }}
                >
                  {item.action.label}
                </button>
              ) : null}
              <button
                type="button"
                className="ui-toast__close"
                aria-label="关闭提示"
                onClick={() => dismiss(item.id)}
              >
                <X size={14} strokeWidth={1.6} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ol>
      ) : null}
    </ToastContext.Provider>
  );
}

const NOOP_TOAST: ToastContextValue = {
  push: () => 0,
  dismiss: () => undefined,
};

/**
 * 发送非阻塞反馈提示；在孤立组件单元测试（无 ToastProvider）中静默退化，避免破坏测试上下文。
 */
export function useToast(): ToastContextValue {
  return useContext(ToastContext) ?? NOOP_TOAST;
}
