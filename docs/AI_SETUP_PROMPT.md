# AI setup prompt

把下面这段话发给 Codex、Claude Code 或其他能读写本地文件并运行终端命令的 AI 助手。

```text
请阅读本仓库的 README.md、SKILL.md 和 docs/EMAIL_PROVIDERS.md。

我要配置一个自动科研简报。请你：

1. 先做“隐私扫描”：在你要推送的仓库文件里搜索邮箱、SMTP 密码、API key、
   学校/单位域名、个人用户名、GitHub 用户名、绝对本机路径。凡是属于我的真实隐私，
   一律改成占位符，再推送。仓库里不允许出现任何一条我的真实邮箱、密钥或用户名。

2. 运行 `python scripts/configure_project.py`，它会逐项向我询问：
   - 收件邮箱；
   - 发件服务（SMTP 或 SendGrid）与 SMTP 参数；
   - 发送时间与所在时区（你负责换算成 UTC 并改 cron）；
   - 我的研究方向、核心课题、方法、可观测性质、2-5 篇种子论文、检索式；
   - 目标期刊、tier1 / tier2 分层、以及 topic gate 的词表（可选，默认是通用
     凝聚态物理范围，我需要换成自己的）；
   - 是否附上海报论文原文 PDF。

3. 我的答案写入 automation/research_brief_config.json —— 这个文件是 gitignored 的，
   只有我本地有，绝对不要推送。

4. 邮箱地址字段在仓库里填占位符 configured-via-github-secret@example.invalid，
   真实值只存在于 GitHub Secrets。

5. 密码、授权码、SendGrid / DeepSeek / Zotero API key 一律用
   `gh secret set` 写入 GitHub Secrets，并回显 secret 名称（不要回显其值）。
   注意：GitHub token 不需要向我索要——复用 gh auth login 已存的凭据。

6. 先运行 `python scripts/generate_research_brief.py --days-back 3 --dry-run`，
   再跑 `python -m unittest discover -s tests -t .`，两者都通过再继续。

7. 推送到我的 GitHub 仓库后，触发一次 workflow_dispatch 测试，第一次
   send_email=false，确认成功后再 send_email=true。

8. 检查 Actions 日志，确认出现 sent email via SMTP 或 sent email via SendGrid。

9. 如果海报 PDF 没进邮件：APS 会以 HTTP 403 拒绝 GitHub Actions 的出口 IP，
   工作流内无解。让我在本机运行 `python .tools/local_fetch_posters.py`，
   它会下载原文 PDF 并推回仓库，然后云端把它折进同一封邮件。

10. 最后再扫一遍已推送的内容，确认仓库里没有我的个人信息；如有，立即改成占位符并重推。
```

隐私规则（必须逐条满足）：

- 我的真实邮箱只能进入 GitHub Secrets，**绝不写进仓库文件、README、config、workflow
  或聊天记录**。
- 邮箱密码、SMTP 授权码、SendGrid / DeepSeek / Zotero API key 同理。
- 仓库里出现的邮箱一律是占位符，占位符的语义是“真值在 GitHub Secret”。
- 如果使用学校或单位邮箱，只使用“客户端授权码 / 应用专用密码”。
- **我的研究方向、期刊分层、topic gate 词表同理**：仓库里的模板只放通用示例，
  我的实际研究画像只存在于 gitignored 的 `automation/research_brief_config.json`。
- 我的 GitHub 用户名和本机绝对路径不能出现在仓库里；脚本通过
  `.tools/repo_config.py` 在运行时解析这些值。
- 生成的文件（简报、论文题录、图片）默认被 .gitignore 忽略，避免暴露研究兴趣。