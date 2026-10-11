# 问题跟踪：GitHub

本仓库的 issue 和 spec 存放在 GitHub Issues 中，所有操作使用 `gh` CLI。

## 操作约定

- **创建 issue**：`gh issue create --title "..." --body "..."`。多行正文使用 heredoc。
- **读取 issue**：`gh issue view <number> --json number,title,body,labels,comments`。
- **列出 issue**：`gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，并按需添加 `--label` 和 `--state` 筛选条件。
- **将 issue 设为父 issue 的子 issue**：`gh issue create --parent <parent> ...`，或之后运行 `gh issue edit <parent> --add-sub-issue <child>`（需要 `gh` 2.94+）。较旧版本可使用 `gh api --method POST repos/<owner>/<repo>/issues/<parent>/sub_issues -F sub_issue_id=<child-db-id>`。如果不能建立子 issue 关系，则在子 issue 正文开头写 `Part of #<parent>`。
- **评论**：`gh issue comment <number> --body "..."`。
- **添加或移除标签**：`gh issue edit <number> --add-label "..."` / `--remove-label "..."`。
- **关闭**：`gh issue close <number> --comment "..."`。

从 `git remote -v` 推断仓库；在克隆目录中运行 `gh` 时会自动使用对应仓库。

## 将 Pull Request 作为需求入口

**将 PR 作为需求入口：否。**如果本仓库以后要把外部 PR 当作功能请求，可将此项改为“是”；`/triage` 会读取该设置。

如果设为“是”，外部 PR 会像 issue 一样进入相同的标签和状态流程，并使用对应的 `gh pr` 命令：

- **读取 PR**：`gh pr view <number> --comments` 和 `gh pr diff <number>`。
- **列出外部 PR 供 triage**：
  `gh api --paginate 'repos/{owner}/{repo}/pulls?state=open' --jq '.[] | select(.author_association | IN("OWNER","MEMBER","COLLABORATOR") | not) | {number, title, author: .user.login, author_association, labels: [.labels[].name]}'`。
- **评论、改标签或关闭**：使用 `gh pr comment`、`gh pr edit --add-label` / `--remove-label`、`gh pr close`。

GitHub 的 issue 和 PR 共用编号。遇到单独的 `#42` 时，先运行 `gh pr view 42`，若不是 PR 再运行 `gh issue view 42`。

## 发布与读取

- 技能要求“发布到 issue tracker”时，创建一个 GitHub issue。
- 技能要求“读取相关 ticket”时，按上文的读取命令查看。

## Wayfinder 操作

以下约定供 `/wayfinder` 使用。map 是一个 issue，child ticket 是它的子 issue。

- **Map**：创建一个带 `wayfinder:map` 标签的 issue，正文包含 Notes / Decisions-so-far / Fog。
- **Child ticket**：创建子 issue（参见上方父子 issue 操作），并添加 `wayfinder:<type>` 标签；`<type>` 为 `research`、`prototype`、`grilling` 或 `task`。未启用子 issue 时，在正文开头写 `Part of #<map>`。认领后，将 ticket 分配给负责推进的开发者。
- **阻塞关系**：优先使用 GitHub 原生 issue dependencies。添加依赖的命令为 `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`。其中 `<blocker-db-id>` 是阻塞 issue 的数据库 ID（可用 `gh api repos/<owner>/<repo>/issues/<n> --jq .id` 获取），不是 `#number` 或 `node_id`。无法使用原生依赖时，在 child 正文开头写 `Blocked by: #<n>, #<n>`。所有阻塞 issue 关闭后，child 才算解除阻塞。
- **Frontier 查询**：列出 map 下仍开放的 child tickets，排除存在未关闭阻塞项或已分配负责人的 ticket；按 map 顺序选择第一个符合条件的 ticket。
- **认领**：运行 `gh issue edit <n> --add-assignee @me`，并将其作为本次流程首次写操作。
- **解决**：运行 `gh issue comment <n> --body "<answer>"`，然后 `gh issue close <n>`，最后在 map 的 Decisions-so-far 中追加一条 context pointer（简要说明及链接）。
