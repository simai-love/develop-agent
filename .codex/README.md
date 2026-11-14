# Agent と GUI の連携

ローカル GUI は Codex CLI と、インストール済みの場合は Claude Code CLI に接続します。01 のチャットは計画を `tasks/<id>/plan.html` に保存し、開発コードを変更しません。実装ボタンと 02 のチャットでは、結果が得られた実行ごとに `report-draft.html` を作り、GUI が `reports/0001.html` 以降に保存します。計画とレポートは図（インライン SVG）と表を含む HTML で、`<script>` と外部リソースを使いません。`plan.html` が無く `plan.md` がある既存タスクは Markdown の計画を使い続け、既存の `reports/*.md`・`result.md` も変換せずに表示します。02 は必要なコード修正と再検証にも対応します。02 では選択式質問を返しません。GUI は実装と追加修正で変わったファイル、およびレポートの一覧を `task.json` に記録します。タスク完了はユーザーが判断します。

Agent は独立して検証できる作業を `child_task_proposals` で提案できます。承認時に GUI が子タスクと専用ブランチを作ります。全子タスクの完了後は Agent が親の総括レポートを作ります。通常の Agent 実行では Git のコミット・マージを行いません。完了操作で人が変更全体を承認した場合だけ、Agent が意味のある単位で一件以上のコミットを作り、子から親、最上位から `dev` へマージして作業ブランチを削除します。GUI は各コミット、マージコミットの親と内容、統合先の状態を照合してから完了を記録します。

タスクの記録は Git 管理から除外した `tasks/` に置き、`app/` や `docs/` に混ぜません。新規タスクは `YYYY-MM-DD_HHMM_タスク名_ID` というフォルダに保存し、既存のID名フォルダは維持します。`tasks/by-name/` は各フォルダへのリンクです。タスクの内容はこのアプリのコミットに含めません。

この端末固有のリポジトリ登録は `repositories.local.json` に置きます。このファイルは Git 管理しません。追加リポジトリのタスク記録はこのアプリの `tasks/<リポジトリID>/` に置き、コード作業には選んだリポジトリのブランチを使用します。

作業の共通指示は [研究開発スキル](skills/research-workflow/SKILL.md)、画面別の指示と JSON Schema は [references](skills/research-workflow/references/README.md) にあります。01・02 のチャットと実装ボタンの追加指示は [app/agent.py](../app/agent.py) にあります。

このアプリ自体の開発に使う実装計画書は [plans/](plans/README.md) に置きます。GUI で作る各研究タスクの計画は `tasks/<id>/plan.html`（既存タスクは `plan.md`）に保存します。
