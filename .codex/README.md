# Agent と GUI の連携

ローカル GUI は Codex CLI と、インストール済みの場合は Claude Code CLI に接続します。01 のチャットは計画を `tasks/<id>/plan.md` に保存し、開発コードを変更しません。実装ボタンと 02 のチャットでは、結果が得られた実行ごとに `report-draft.md` を作り、GUI が `reports/0001.md` 以降に保存します。02 は必要なコード修正と再検証にも対応します。02 では選択式質問を返しません。GUI は実装と追加修正で変わったファイル、およびレポートの一覧を `task.json` に記録します。タスク完了はユーザーが判断します。

タスクの記録は Git 管理から除外した `tasks/` に置き、`app/` や `docs/` に混ぜません。タスクの内容はこのアプリのコミットに含めません。

作業の共通指示は [研究開発スキル](skills/research-workflow/SKILL.md)、画面別の指示と JSON Schema は [references](skills/research-workflow/references/README.md) にあります。01・02 のチャットと実装ボタンの追加指示は [app/agent.py](../app/agent.py) にあります。
