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
