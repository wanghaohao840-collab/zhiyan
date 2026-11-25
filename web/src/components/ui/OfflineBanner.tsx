import { Wifi, WifiOff } from "lucide-react";
import { useEffect, useState } from "react";

/**
 * 网络连接状态顶部提示条：仅在浏览器进入离线状态或刚恢复连线时轻量显现，常态零占位。
 */
export function OfflineBanner() {
  const [status, setStatus] = useState<"online" | "offline" | "reconnected">(() =>
    typeof navigator !== "undefined" && navigator.onLine === false ? "offline" : "online",
  );

  useEffect(() => {
    let reconnectTimer: number | undefined;

    function handleOffline() {
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      setStatus("offline");
    }

    function handleOnline() {
      setStatus("reconnected");
      reconnectTimer = window.setTimeout(() => setStatus("online"), 3200);
    }

    window.addEventListener("offline", handleOffline);
    window.addEventListener("online", handleOnline);
    return () => {
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      window.removeEventListener("offline", handleOffline);
      window.removeEventListener("online", handleOnline);
    };
  }, []);

  if (status === "online") return null;

  return (
    <div
      className={`offline-banner offline-banner--${status}`}
      role={status === "offline" ? "alert" : "status"}
      aria-live={status === "offline" ? "assertive" : "polite"}
    >
      {status === "offline" ? (
        <>
          <WifiOff size={15} strokeWidth={1.6} aria-hidden="true" />
          <span>当前网络连接已断开，待网络恢复后可重试未完成操作。</span>
        </>
      ) : (
        <>
          <Wifi size={15} strokeWidth={1.6} aria-hidden="true" />
          <span>网络已恢复连接。</span>
        </>
      )}
    </div>
  );
}
