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
| `app/agent.py`・`app/agent_stream.py`・`app/claude_response.py` | CLI の起動、進捗表示、中断、応答の検証 |
| `app/storage.py`・`app/evidence.py` | タスクと検証資料の保存 |
| `.codex/skills/research-workflow/` | Agent に渡す画面別の作業規則と応答形式 |

リポジトリ登録画面の候補は `/api/repository-candidates` から取得します。`~/prog` 直下のGitルートのうち、ローカルの `dev` ブランチを持ち、まだ登録されていないものだけを返します。手入力での登録も従来どおり可能です。

## タスクと Git

`POST /api/repositories` は Git ルートと `dev` ブランチを確認し、新しい作業先を登録します。

`GET /api/repositories` は登録済みの作業先を返します。各タスク API は `repo` クエリで作業先を選びます。省略時は `develop-agent` です。既存の自アプリタスクは `tasks/<id>/`、追加リポジトリのタスクは `tasks/<リポジトリID>/<id>/` に保存します。`gpu_solver` を選ぶと、タスク記録はこのアプリに置いたまま、そのリポジトリの Git ブランチと worktree を操作します。GPU 側から移した 11 件のタスクは `tasks/gpu_solver/` にあり、元の `gpu_solver/tasks/` はバックアップとして保持します。移行時には記録内の絶対パスとタスク間シンボリックリンクを中央保存先に更新しました。登録は Git 管理外の `.codex/repositories.local.json` に置きます。GPU 登録で WSL の `/dev/dxg` がある場合は、Codex サンドボックス設定と HPC SDK の環境変数を Agent の子プロセスへ渡します。登録先を一覧で開くだけでは、既存タスクの回復処理や移行処理を走らせません。

`POST /api/tasks` は最上位タスクと `dev` からの専用ブランチを作ります。`POST /api/tasks/<id>/children` は親ブランチから子の専用ブランチを作ります。各タスクの `task.json` は `parent_id`、`root_id`、`branch`、作業の状態、コミット ID を持ちます。GUI が管理するタスクの記録は `tasks/` に置き、Git から除外します。

葉タスクの Agent 実装は、変更を専用 worktree に残し、レポート原稿を `tasks/<id>/report-draft.md` に作ります。GUI が成功した原稿を `reports/0001.md` 以降に保存します。通常の Agent 実行はコミットとマージを行いません。完了操作では、確認済みの変更全体を Agent が一件以上のコミットに分けます。GUI は各コミットの対象と最終ツリーを照合し、子なら親へ、最上位なら `dev` へ `--no-ff` で統合します。コミット実行ごとに、ユーザーのコミット方針と、結果・作成したコミット・Agent の報告を 02 の `chat_result.json` に追記します。失敗または中断した場合は、CLI 出力の末尾と標準エラーを `tasks/<タスクID>/diagnostics/commit-<実行ID>.json` に保存します。子を持つ親は全子の完了と最新の総括が必要です。

既存の `projects/` は起動時に読み取り、所属タスクの `root_id` と各作業ブランチを整えます。元の記録は控えとして残し、新しい操作では参照しません。古い Git 操作記録の回復は移行より先に実行します。

## 画面と Agent

01 のチャットは計画と実装可否を更新します。「実装に進む」は任意の追加指示とともに実装・初回検証を開始します。02 のチャットは結果への質問、追加検証、計画内の修正と再検証を受け付けます。実行 ID ごとの状態は `/api/runs/<id>` で取得し、`/api/runs/<id>/cancel` で中断を要求します。途中のコード変更は残し、未完成のレポートを確定しません。

Agent の入力には会話の直近分、過去レポートの抜粋、最近の資料名を渡します。全文と原本は保存先を示し、必要なものだけ開くよう指示します。これにより、画像や過去レポートが増えても初期入力が無制限に膨らみません。画面の資料画像は遅延読み込みし、サーバーは大きな資料を順次配信します。

Claude Code の最終イベントでは `structured_output` を優先し、なければ `result` の JSON 文字列を読み込みます。有効な JSON オブジェクトが得られない場合は、原因と再実行前に変更を確認する案内をチャットに表示します。CLI 出力の末尾と標準エラーは `tasks/<タスクID>/diagnostics/claude-<実行ID>.json` に保存します。追加リポジトリのタスクでは `tasks/<リポジトリID>/<タスクID>/diagnostics/` に保存します。

01・02 のチャット画像は既存の資料 API で `evidence/` に保存し、`chat_plan.json` または `chat_result.json` の該当ユーザーメッセージにファイル名を記録します。送信時には同じタスクの画像か検証し、最大4枚・各10 MBを適用します。Codex CLI には現在の依頼に付いた画像を `--image` で渡します。Claude Code CLI には保存先を会話に示し、Read ツールで開くよう指示します。

Claude Code CLI は `claude --print --output-format stream-json --json-schema` で呼び出し、最終応答を Codex と同じ形式の JSON として扱います。モデルは `fable`・`opus`・`sonnet`・`haiku` の別名か、空欄の既定（CLI 設定）から選びます。表示名のバージョン（例: Opus 5.5）は、サーバーが初回に各別名で CLI を起動し、`init` イベントの解決済みモデル ID から作ります。結果はサーバーの再起動まで保持し、タスクには別名を保存するため新しいバージョンにも追従します。権限は Codex のサンドボックス設定に合わせます。`workspace-write` では Claude Code の Bash サンドボックスを必須にし、サンドボックス内のコマンドを自動許可します。ネットワークは `app/agent.py` の `CLAUDE_ALLOWED_DOMAINS` と、環境変数 `DEVELOP_AGENT_CLAUDE_ALLOWED_DOMAINS`（カンマ区切り）のドメインだけに接続できます。Linux では `bubblewrap` と `socat` が必要で、無い場合は実行せずに理由を表示します。`danger-full-access` では権限確認をすべて省略します。どちらもスキルの手順、タスク保存先、必要に応じて開発コードとコミット用の Git 共通ディレクトリを `--add-dir` で許可します。

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
