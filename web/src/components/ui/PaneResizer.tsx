import { useCallback, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";

export interface UseResizableSplitOptions {
  storageKey: string;
  defaultLeft: number;
  defaultRight: number;
  minLeft: number;
  maxLeft: number;
  minRight: number;
  maxRight: number;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Math.round(value)));
}

function readPersistedWidth(key: string, fallback: number, min: number, max: number): number {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? clamp(parsed, min, max) : fallback;
  } catch {
    return fallback;
  }
}

/**
 * 三栏工作台可拖拽宽度状态：仅在用户显式拖动或按键调整后才写入轻量偏好，
 * 默认首次访问零 localStorage 写入，兼容严格存储隔离契约。
 */
export function useResizableSplit({
  storageKey,
  defaultLeft,
  defaultRight,
  minLeft,
  maxLeft,
  minRight,
  maxRight,
}: UseResizableSplitOptions) {
  const leftKey = `${storageKey}.left`;
  const rightKey = `${storageKey}.right`;
  const [leftWidth, setLeftWidth] = useState(() =>
    readPersistedWidth(leftKey, defaultLeft, minLeft, maxLeft),
  );
  const [rightWidth, setRightWidth] = useState(() =>
    readPersistedWidth(rightKey, defaultRight, minRight, maxRight),
  );

  const persist = useCallback((key: string, value: number) => {
    try {
      window.localStorage.setItem(key, String(value));
    } catch {
      // 忽略无痕/受限存储异常
    }
  }, []);

  const updateLeft = useCallback(
    (next: number) => {
      const clamped = clamp(next, minLeft, maxLeft);
      setLeftWidth(clamped);
      persist(leftKey, clamped);
    },
    [leftKey, maxLeft, minLeft, persist],
  );

  const updateRight = useCallback(
    (next: number) => {
      const clamped = clamp(next, minRight, maxRight);
      setRightWidth(clamped);
      persist(rightKey, clamped);
    },
    [maxRight, minRight, persist, rightKey],
  );

  const resetLeft = useCallback(() => {
    setLeftWidth(defaultLeft);
    try {
      window.localStorage.removeItem(leftKey);
    } catch {
      // 忽略异常
    }
  }, [defaultLeft, leftKey]);

  const resetRight = useCallback(() => {
    setRightWidth(defaultRight);
    try {
      window.localStorage.removeItem(rightKey);
    } catch {
      // 忽略异常
    }
  }, [defaultRight, rightKey]);

  return {
    leftWidth,
    rightWidth,
    updateLeft,
    updateRight,
    resetLeft,
    resetRight,
  };
}

export interface PaneResizerProps {
  label: string;
  value: number;
  min: number;
  max: number;
  /** 1 表示向右拖动增大目标栏宽（左栏手柄），-1 表示向左拖动增大目标栏宽（右栏手柄） */
  direction: 1 | -1;
  onChange: (next: number) => void;
  onReset: () => void;
}

/**
 * 三栏工作台极细 1px 可拖拽分隔条（宽热区 + 双击复原 + 键盘左右方向键微调）。
 */
export function PaneResizer({
  label,
  value,
  min,
  max,
  direction,
  onChange,
  onReset,
}: PaneResizerProps) {
  const dragStartRef = useRef<{ x: number; startWidth: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  function handlePointerDown(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    dragStartRef.current = { x: event.clientX, startWidth: value };
    setDragging(true);
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  function handlePointerMove(event: PointerEvent<HTMLDivElement>) {
    const start = dragStartRef.current;
    if (!start) return;
    const delta = (event.clientX - start.x) * direction;
    onChange(start.startWidth + delta);
  }

  function handlePointerUp(event: PointerEvent<HTMLDivElement>) {
    if (!dragStartRef.current) return;
    dragStartRef.current = null;
    setDragging(false);
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const step = event.shiftKey ? 32 : 16;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      onChange(value - step * direction);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      onChange(value + step * direction);
    } else if (event.key === "Home") {
      event.preventDefault();
      onReset();
    }
  }

  return (
    <div
      role="separator"
      tabIndex={0}
      aria-label={label}
      aria-orientation="vertical"
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={value}
      title={`${label}（拖动调节，双击恢复默认）`}
      className={`pane-resizer${dragging ? " is-dragging" : ""}`}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
      onDoubleClick={onReset}
      onKeyDown={handleKeyDown}
    />
  );
}
