# Agent Skills Directory

此目錄由 FileAgentSkillsProvider 監控。
每個子目錄代表一個 Skill，必須包含 SKILL.md 檔案。

## 目錄結構範例
```
skills/
  my-skill/
    SKILL.md           ← 必要：含 YAML frontmatter
    templates/          ← 選用：參數化模板
    references/         ← 選用：補充文件
    examples/           ← 選用：使用範例
```

## SKILL.md 格式
```markdown
---
name: my-skill
description: "描述這個 skill 做什麼"
---

# My Skill
...
```

Gatekeeper 產出 Skill 後會自動寫入此目錄。
