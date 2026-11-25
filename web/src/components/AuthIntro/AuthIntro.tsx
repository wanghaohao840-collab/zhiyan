import cosmosHorizon from "../../assets/cosmos-horizon.jpg";
import knowledgePages from "../../assets/knowledge-pages.png";
import { useTheme } from "../../theme/ThemeProvider";
import { BrandMark } from "../BrandMark/BrandMark";

/**
 * 认证页品牌展示区：根据当前主题 1:1 呈现 Style A（极简知识感·浮空书页）
 * 与 Style B（深色 AI 感·深空日蚀光环）两套主视觉。
 */
export function AuthIntro() {
  const { theme } = useTheme();
  const isMidnight = theme === "midnight";

  return (
    <aside className="auth-intro" aria-label="知研介绍">
      <div className="auth-intro__brand">
        <BrandMark />
        <span className="auth-intro__brand-copy">
          <strong>知研</strong>
          <small>让知识在时间中沉淀</small>
        </span>
      </div>
      <div className="auth-intro__hero">
        <p className="auth-intro__eyebrow">
          {isMidnight ? "智能 · 推理 · 记忆 · 无限可能" : "阅读 · 思考 · 连接 · 长期积累"}
        </p>
        <h2>
          {isMidnight ? (
            <>
              与文档对话
              <br />
              让知识在时间中沉淀
            </>
          ) : (
            <>
              让每一次阅读
              <br />
              成为长期的知识资产
            </>
          )}
        </h2>
        <p>
          {isMidnight ? (
            <>
              基于 Memory + RAG 构建你的个人 AI 学习伙伴，
              <br />
              从阅读，到理解，到连接，再到新的洞察。
            </>
          ) : (
            <>
              与文档对话，留下有来源的笔记，
              <br />
              让学习在时间中持续积累。
            </>
          )}
        </p>
        <div className="auth-intro__art-frame">
          <img
            className="auth-intro__art"
            src={isMidnight ? cosmosHorizon : knowledgePages}
            alt=""
            width={isMidnight ? 1600 : 1536}
            height={isMidnight ? 900 : 1024}
          />
        </div>
        <div className="auth-intro__caption">
          <span>
            {isMidnight ? "在时间的维度上，知识拥有了新的生命。" : "知识，是时间的复利。"}
          </span>
          <em lang="en">
            {isMidnight ? "Knowledge lives longer than a moment." : "Knowledge grows with time."}
          </em>
        </div>
      </div>
      <p className="auth-intro__mobile-tagline">每一次阅读，都成为更好的自己。</p>
    </aside>
  );
}
