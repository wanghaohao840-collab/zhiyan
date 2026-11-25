import { AlertTriangle, RotateCcw } from "lucide-react";
import { Component, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
}

/**
 * 全局双主题容错边界：捕获渲染期未处理异常，提供安全恢复与 /legacy/ 回滚入口，不暴露内部堆栈。
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { hasError: true };
  }

  private handleReset = () => {
    this.setState({ hasError: false });
  };

  render() {
    if (!this.state.hasError) {
      return this.props.children;
    }

    return (
      <main className="error-boundary" role="alert">
        <section className="error-boundary__card">
          <span className="error-boundary__icon" aria-hidden="true">
            <AlertTriangle size={22} strokeWidth={1.6} />
          </span>
          <h1>页面暂时遇到意外</h1>
          <p>你的学习数据仍然安全保存。可以尝试重新渲染当前视图、返回概览或前往旧版界面继续操作。</p>
          <div className="error-boundary__actions">
            <button type="button" className="button button--primary button--md" onClick={this.handleReset}>
              <RotateCcw size={15} strokeWidth={1.6} aria-hidden="true" />
              重新加载视图
            </button>
            <a className="button button--secondary button--md" href="/overview">
              返回概览
            </a>
            <a className="button button--ghost button--md" href="/legacy/">
              前往旧版
            </a>
          </div>
        </section>
      </main>
    );
  }
}
