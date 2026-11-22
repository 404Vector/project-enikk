# GitHub 저장소 설정

저장소 설정은 GitOps 대상이 아니다. 이 디렉터리의 파일이 원본이며, 소유자 확인을 받아 `gh api` 로 적용한다(ADR-0020).
이 디렉터리는 보호 경로다(I-20).

| 파일 | 내용 |
|---|---|
| `ruleset-main.json` | `main`: PR 필수, 필수 검사(`checks`, `protected-paths`), 강제 push·삭제 금지, 예외 없음 |
| `ruleset-agent-branches.json` | `main` 과 `agent/**` 밖의 브랜치는 저장소 관리자만 만들고 바꾼다. GitHub App 은 `agent/**` 에만 push 할 수 있다 |

- `integration_id` 15368 은 GitHub Actions 다. 승인 흐름이 동작하면 `enikk/owner-approval`(출처: App `enikk-deployer`, ID 5869950)을 `ruleset-main.json` 의 필수 검사에 추가한다.
- `actor_id` 5 는 저장소 역할 admin 이다.

## 적용

```bash
repo=404Vector/project-enikk
# main: 기존 ruleset 을 덮어쓴다
id=$(gh api "repos/$repo/rulesets" --jq '.[] | select(.name == "protect-main") | .id')
gh api -X PUT "repos/$repo/rulesets/$id" --input deploy/github/ruleset-main.json
# agent 브랜치: 없으면 만들고, 있으면 덮어쓴다
id=$(gh api "repos/$repo/rulesets" --jq '.[] | select(.name == "agent-branches-only") | .id')
if [ -z "$id" ]; then
  gh api -X POST "repos/$repo/rulesets" --input deploy/github/ruleset-agent-branches.json
else
  gh api -X PUT "repos/$repo/rulesets/$id" --input deploy/github/ruleset-agent-branches.json
fi
# auto-merge 허용, 머지된 브랜치 자동 삭제
gh api -X PATCH "repos/$repo" -F allow_auto_merge=true -F delete_branch_on_merge=true
```
