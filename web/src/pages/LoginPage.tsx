import { Eye, EyeOff } from "lucide-react";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "../api/client";
import { resolveAuthDestination, useAuth } from "../auth/AuthProvider";
import { AuthIntro } from "../components/AuthIntro/AuthIntro";
import { OverlayDialog, ThemeSwitcher } from "../components/ui";

type LocationState = { from?: unknown; sessionExpired?: unknown };

function SessionExpiredDialog({ onClose }: { onClose: () => void }) {
  const primaryActionRef = useRef<HTMLButtonElement>(null);

  return (
    <OverlayDialog
      open
      hideHeader
      dismissible={false}
      ariaLabel="会话已过期"
      layerClassName="session-dialog-layer"
      panelClassName="session-dialog"
      initialFocusRef={primaryActionRef}
      returnFocusTo={null}
      onClose={onClose}
    >
      <h2 id="session-dialog-title">会话已过期</h2>
      <p>为保护你的学习数据，本次会话已结束。请重新登录后继续。</p>
      <div className="session-dialog__actions">
        <button className="session-dialog__secondary" type="button" onClick={onClose}>
          退出登录
        </button>
        <button ref={primaryActionRef} type="button" onClick={onClose}>
          重新登录
        </button>
      </div>
    </OverlayDialog>
  );
}

export function LoginPage() {
  const auth = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const locationState = location.state as LocationState | null;
  const [error, setError] = useState<ApiError>();
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [sessionExpiredOpen, setSessionExpiredOpen] = useState(
    locationState?.sessionExpired === true,
  );
  const usernameRef = useRef<HTMLInputElement>(null);
  const sessionExpiredWasOpen = useRef(sessionExpiredOpen);
  const from = locationState?.from;
  const intendedPath = typeof from === "string" ? from : undefined;

  useEffect(() => {
    if (!sessionExpiredOpen && sessionExpiredWasOpen.current) {
      usernameRef.current?.focus();
    }
    sessionExpiredWasOpen.current = sessionExpiredOpen;
  }, [sessionExpiredOpen]);

  if (auth.status === "loading") {
    return <div className="session-status" role="status">正在恢复会话…</div>;
  }
  if (auth.status === "authenticated") {
    return <Navigate to={resolveAuthDestination(intendedPath)} replace />;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(undefined);
    const form = new FormData(event.currentTarget);

    try {
      await auth.login(
        {
          username: String(form.get("username") ?? ""),
          password: String(form.get("password") ?? ""),
        },
        intendedPath,
      );
    } catch (reason) {
      setError(
        reason instanceof ApiError
          ? reason
          : new ApiError(0, "client_error", "服务暂时不可用，请稍后重试"),
      );
    }
  }

  const usernameError = error?.fieldErrors.username;
  const passwordError = error?.fieldErrors.password;

  function dismissSessionExpired() {
    setSessionExpiredOpen(false);
    navigate("/login", {
      replace: true,
      state: intendedPath ? { from: intendedPath } : undefined,
    });
  }

  return (
    <main className="auth-page">
      <AuthIntro />
      <section className="auth-card" aria-labelledby="login-title">
        <h1 id="login-title">登录</h1>
        {error ? <div className="form-banner" role="alert">{error.message}</div> : null}
        <form onSubmit={handleSubmit}>
          <div className="form-field">
            <label htmlFor="login-username">用户名</label>
            <input
              ref={usernameRef}
              id="login-username"
              name="username"
              autoComplete="username"
              spellCheck={false}
              required
              minLength={3}
              maxLength={32}
              aria-invalid={Boolean(usernameError)}
              aria-describedby={`login-username-help${usernameError ? " login-username-error" : ""}`}
            />
            <p id="login-username-help" className="field-help">请输入 3–32 个字符的用户名</p>
            {usernameError ? <p id="login-username-error" className="field-error">{usernameError}</p> : null}
          </div>
          <div className="form-field">
            <label htmlFor="login-password">密码</label>
            <div className="password-field">
              <input
                id="login-password"
                name="password"
                type={passwordVisible ? "text" : "password"}
                autoComplete="current-password"
                required
                minLength={8}
                maxLength={128}
                aria-invalid={Boolean(passwordError)}
                aria-describedby={`login-password-help${passwordError ? " login-password-error" : ""}`}
              />
              <button
                className="password-field__toggle"
                type="button"
                aria-label={passwordVisible ? "隐藏密码" : "显示密码"}
                aria-pressed={passwordVisible}
                onClick={() => setPasswordVisible((visible) => !visible)}
              >
                {passwordVisible ? (
                  <EyeOff size={18} strokeWidth={1.6} aria-hidden="true" />
                ) : (
                  <Eye size={18} strokeWidth={1.6} aria-hidden="true" />
                )}
              </button>
            </div>
            <p id="login-password-help" className="field-help">请输入 8–128 个字符的密码</p>
            {passwordError ? <p id="login-password-error" className="field-error">{passwordError}</p> : null}
          </div>
          <button className="auth-submit" type="submit" disabled={auth.isLoginPending}>
            {auth.isLoginPending ? "登录中…" : "登录"}
          </button>
        </form>
        <p className="auth-switch">
          还没有账号？{" "}
          <Link
            to="/register"
            state={intendedPath ? { from: intendedPath } : undefined}
          >
            创建账号
          </Link>
        </p>
      </section>
      {/* NOTE: 保持固定视觉定位于右上角，同时让键盘 Tab 优先进入登录表单主任务流 */}
      <div className="auth-topbar">
        <ThemeSwitcher />
      </div>
      {sessionExpiredOpen ? <SessionExpiredDialog onClose={dismissSessionExpired} /> : null}
    </main>
  );
}
