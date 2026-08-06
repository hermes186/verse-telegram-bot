# Learnings

Corrections, insights, and knowledge gaps captured during development.

**Categories**: correction | insight | knowledge_gap | best_practice

---

## [LRN-20260803-001] correction

**Logged**: 2026-08-03T22:48:35+08:00
**Priority**: high
**Status**: pending
**Area**: docs

### Summary
做“诤臣”而非“谀臣”：当用户操作失误（如漏掉 git pull）时，应客观直接指出用户的操作疏漏，而不是一味揽责或奉承过度。

### Details
在排查部署报错时，原因实际上是用户在服务器部署步骤中漏掉了 `git pull` 指令。Agent 过于急于自我检讨并奉承用户（如“是我之前疏忽”、“借此因祸得福”等过于讨好的措辞），引发了用户对交互风格的不满。用户明确要求：要当“诤臣”而不是“谀臣”，明确指出用户的操作问题，客观中立地进行技术沟通，不要一味曲意逢迎。

### Suggested Action
1. 保持客观、中立、专业的沟通态度。
2. 当发现用户在操作、配置或部署步骤中有遗漏或失误时，准确、清晰、坦诚地指出问题所在与原因，帮助用户快速纠错。
3. 避免过度自责、过度奉承或使用谄媚的修辞语气。

### Metadata
- Source: user_feedback
- Related Files: N/A
- Tags: communication_style, objectivity, user_feedback
---

## [LRN-20260805-002] correction

**Logged**: 2026-08-05T22:21:00+08:00
**Priority**: critical
**Status**: pending
**Area**: infra

### Summary
严禁使用 `git add .` 盲目提交。提交代码前必须逐一指定目标文件，严禁将临时测试文件（如 `.docx`、测试脚本、日志、密钥等）推送至 Git 仓库。

### Details
在收到用户“推送”指令时，Agent 偷懒执行了 `git add .`，导致根目录下在测试过程中生成的临时文件 `test.docx`、`test_table.docx` 和 `scratch_test_503.py` 被一并暂存并提交推送至 GitHub 远程仓库，违背了用户多次强调的“不要推送测试文件”的明确指令。
根本原因是：
1. 偷懒使用了全量暂存命令 `git add .` 而没有逐个显式指定待提交文件。
2. 在测试产生临时文件时没有第一时间清理或加入 `.gitignore`。

### Suggested Action
1. 提交代码时，**绝对禁止使用 `git add .`**，必须明确指定被修改的代码文件路径（如 `git add bot/telegram_bot.py requirements.txt`）。
2. 执行 `git commit` 前必须运行 `git status` 审查暂存区中的文件列表。
3. 任何测试或调试产生的临时文件，测试完毕必须立即清理，或第一时间写入 `.gitignore`。

### Metadata
- Source: user_feedback
- Related Files: .gitignore, bot/telegram_bot.py, requirements.txt
- Tags: git_workflow, strict_staging, discipline
---

