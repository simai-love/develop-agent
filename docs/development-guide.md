# 開発・システム設計ガイド

この文書は、画面やタスクの実行経路を変更する開発者向けです。利用者の操作は[利用ガイド](user-guide.md)にまとめています。ここでいう worktree は、ブランチごとのコードを別のディレクトリに置く Git の仕組みです。ブランチは変更履歴を分けるための参照、コミットは変更を履歴に保存する単位です。

## 構成

| 場所 | 役割 |
| --- | --- |
| `app/server.py` | ローカル HTTP API と静的ファイルの配信 |
| `app/repositories.py` | 端末固有のリポジトリ登録と、選択先ごとのタスクサービス |
| `app/static/` | タスク一覧、計画、チャット、レポートの画面 |
| `app/task_workflow.py` | タスク専用ブランチ、完了、統合、既存記録の移行 |
| `app/git/workspace.py` | Git のブランチ、worktree、差分、コミット、`--no-ff` マージ |
| `app/agent.py`・`app/agent_stream.py` | CLI の起動、進捗表示、中断、応答の検証 |
| `app/storage.py`・`app/evidence.py` | タスクと検証資料の保存 |
| `.codex/skills/research-workflow/` | Agent に渡す画面別の作業規則と応答形式 |

## タスクと Git

`POST /api/repositories` は Git ルートと `dev` ブランチを確認し、新しい作業先を登録します。

`GET /api/repositories` は登録済みの作業先を返します。各タスク API は `repo` クエリで作業先を選びます。省略時は `develop-agent` です。既存の自アプリタスクは `tasks/<id>/`、追加リポジトリのタスクは `tasks/<リポジトリID>/<id>/` に保存します。`gpu_solver` を選ぶと、タスク記録はこのアプリに置いたまま、そのリポジトリの Git ブランチと worktree を操作します。GPU 側から移した 11 件のタスクは `tasks/gpu_solver/` にあり、元の `gpu_solver/tasks/` はバックアップとして保持します。移行時には記録内の絶対パスとタスク間シンボリックリンクを中央保存先に更新しました。登録は Git 管理外の `.codex/repositories.local.json` に置きます。GPU 登録で WSL の `/dev/dxg` がある場合は、Codex サンドボックス設定と HPC SDK の環境変数を Agent の子プロセスへ渡します。登録先を一覧で開くだけでは、既存タスクの回復処理や移行処理を走らせません。

`POST /api/tasks` は最上位タスクと `dev` からの専用ブランチを作ります。`POST /api/tasks/<id>/children` は親ブランチから子の専用ブランチを作ります。各タスクの `task.json` は `parent_id`、`root_id`、`branch`、作業の状態、コミット ID を持ちます。GUI が管理するタスクの記録は `tasks/` に置き、Git から除外します。

葉タスクの Agent 実装は、変更を専用 worktree に残し、レポート原稿を `tasks/<id>/report-draft.md` に作ります。GUI が成功した原稿を `reports/0001.md` 以降に保存します。通常の Agent 実行はコミットとマージを行いません。完了操作では、確認済みの変更全体を Agent が一件以上のコミットに分けます。GUI は各コミットの対象と最終ツリーを照合し、子なら親へ、最上位なら `dev` へ `--no-ff` で統合します。子を持つ親は全子の完了と最新の総括が必要です。

既存の `projects/` は起動時に読み取り、所属タスクの `root_id` と各作業ブランチを整えます。元の記録は控えとして残し、新しい操作では参照しません。古い Git 操作記録の回復は移行より先に実行します。

## 画面と Agent

01 のチャットは計画と実装可否を更新します。「実装に進む」は任意の追加指示とともに実装・初回検証を開始します。02 のチャットは結果への質問、追加検証、計画内の修正と再検証を受け付けます。実行 ID ごとの状態は `/api/runs/<id>` で取得し、`/api/runs/<id>/cancel` で中断を要求します。途中のコード変更は残し、未完成のレポートを確定しません。

Agent の入力には会話の直近分、過去レポートの抜粋、最近の資料名を渡します。全文と原本は保存先を示し、必要なものだけ開くよう指示します。これにより、画像や過去レポートが増えても初期入力が無制限に膨らみません。画面の資料画像は遅延読み込みし、サーバーは大きな資料を順次配信します。

## Markdown と数式

`app/static/markdown.mjs` は許可したリンクと画像だけを DOM（画面を構成する要素の木構造）に追加します。`../evidence/<filename>` は当該タスクの資料 API（画面からサーバーへデータを求める窓口）へ変換されます。数式は KaTeX（文字で数式を書く LaTeX 記法を画面に描くライブラリ）で描画します。文中の `$...$` と独立した `$$...$$` を扱い、HTML を直接解釈しません。KaTeX は `app/cli/package-lock.json` で固定し、`/vendor/katex/` から配信します。

## 開発と確認

Python は `uv sync --frozen` で `.venv` を用意し、有効化してからデバッグします。JavaScript の依存関係は `npm ci --prefix app/cli` で導入します。基本確認は次を実行します。

```bash
source .venv/bin/activate
python -m unittest discover -s tests -q
node --test tests/test_markdown.mjs
```

Git 操作を変更した場合は、隔離した一時リポジトリで子から親、親から `dev` への統合と移行のテストを確認してください。
