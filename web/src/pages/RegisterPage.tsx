import { Eye, EyeOff } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Link, Navigate, useLocation } from "react-router-dom";

import { ApiError } from "../api/client";
import { resolveAuthDestination, useAuth } from "../auth/AuthProvider";
import { AuthIntro } from "../components/AuthIntro/AuthIntro";

type LocationState = { from?: unknown };

export function RegisterPage() {
  const auth = useAuth();
  const location = useLocation();
  const [error, setError] = useState<ApiError>();
  const [passwordVisible, setPasswordVisible] = useState(false);
  const from = (location.state as LocationState | null)?.from;
  const intendedPath = typeof from === "string" ? from : undefined;

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
      await auth.register(
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
          : new ApiError(0, "client_error", "注册失败，请稍后重试"),
      );
    }
  }

  const usernameError = error?.fieldErrors.username;
  const passwordError = error?.fieldErrors.password;

  return (
    <main className="auth-page">
      <AuthIntro />
      <section className="auth-card" aria-labelledby="register-title">
        <h1 id="register-title">注册</h1>
        {error ? <div className="form-banner" role="alert">{error.message}</div> : null}
        <form onSubmit={handleSubmit}>
          <div className="form-field">
            <label htmlFor="register-username">用户名</label>
            <input
              id="register-username"
              name="username"
              autoComplete="username"
              spellCheck={false}
              required
              minLength={3}
              maxLength={32}
              aria-invalid={Boolean(usernameError)}
              aria-describedby={`register-username-help${usernameError ? " register-username-error" : ""}`}
            />
            <p id="register-username-help" className="field-help">请输入 3–32 个字符的用户名</p>
            {usernameError ? <p id="register-username-error" className="field-error">{usernameError}</p> : null}
          </div>
          <div className="form-field">
            <label htmlFor="register-password">密码</label>
            <div className="password-field">
              <input
                id="register-password"
                name="password"
                type={passwordVisible ? "text" : "password"}
                autoComplete="new-password"
                required
                minLength={8}
                maxLength={128}
                aria-invalid={Boolean(passwordError)}
                aria-describedby={`register-password-help${passwordError ? " register-password-error" : ""}`}
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
            <p id="register-password-help" className="field-help">请输入 8–128 个字符的密码</p>
            {passwordError ? <p id="register-password-error" className="field-error">{passwordError}</p> : null}
          </div>
          <button className="auth-submit" type="submit" disabled={auth.isRegisterPending}>
            {auth.isRegisterPending ? "注册中…" : "注册"}
          </button>
        </form>
        <p className="auth-switch">
          已有账号？{" "}
          <Link
            to="/login"
            state={intendedPath ? { from: intendedPath } : undefined}
          >
            返回登录
          </Link>
        </p>
      </section>
    </main>
  );
}
