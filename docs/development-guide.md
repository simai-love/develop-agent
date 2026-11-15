# 開発・システム設計ガイド

この文書は、画面やタスクの実行経路を変更する開発者向けです。利用者の操作は[利用ガイド](user-guide.md)にまとめています。ここでいう worktree は、ブランチごとのコードを別のディレクトリに置く Git の仕組みです。ブランチは変更履歴を分けるための参照、コミットは変更を履歴に保存する単位です。

## 構成

| 場所 | 役割 |
| --- | --- |
| `app/server.py` | ローカル HTTP API と静的ファイルの配信 |
| `app/repositories.py` | 端末固有のリポジトリ登録と、選択先ごとのタスクサービス |
| `app/record_index.py` | タスクと旧プロジェクトを日時・名称から探すリンク |
| `app/static/` | タスク一覧、計画、チャット、レポートの画面 |
| `app/task_workflow.py` | タスク専用ブランチ、完了、統合、既存記録の移行 |
| `app/git/workspace.py` | Git のブランチ、worktree、差分、コミット、`--no-ff` マージ |
| `app/agent.py`・`app/agent_stream.py`・`app/claude_response.py` | CLI の起動、進捗表示、中断、応答の検証 |
| `app/storage.py`・`app/evidence.py` | タスクと検証資料の保存 |
| `app/file_browser.py`・`app/static/files.mjs` | プロジェクト内ファイルの閲覧（読み取り専用） |
| `.codex/skills/research-workflow/` | Agent に渡す画面別の作業規則と応答形式 |
| `app/home.py`・`app/memos.py`・`app/plan_approval.py` | ホームの集計、メモ、担当 Agent 設定、計画の承認 |
| `app/scheduler.py`・`app/nightly.py`・`app/daily_report.py` | 0:00 の夜間実行と 6:00 の日報 |
| `app/task_status.py` | タスクの段階表示 |

リポジトリ登録画面の候補は `/api/repository-candidates` から取得します。`~/prog` 直下のGitルートのうち、ローカルの `dev` ブランチを持ち、まだ登録されていないものだけを返します。手入力での登録も従来どおり可能です。

## タスクと Git

`POST /api/repositories` は Git ルートと `dev` ブランチを確認し、新しい作業先を登録します。

`GET /api/repositories` は登録済みの作業先を返します。各タスク API は `repo` クエリで作業先を選びます。省略時は `develop-agent` です。既存の自アプリタスクは `tasks/<id>/`、追加リポジトリのタスクは `tasks/<リポジトリID>/<id>/` に保存します。`gpu_solver` を選ぶと、タスク記録はこのアプリに置いたまま、そのリポジトリの Git ブランチと worktree を操作します。GPU 側から移した 11 件のタスクは `tasks/gpu_solver/` にあり、元の `gpu_solver/tasks/` はバックアップとして保持します。移行時には記録内の絶対パスとタスク間シンボリックリンクを中央保存先に更新しました。登録は Git 管理外の `.codex/repositories.local.json` に置きます。GPU 登録で WSL の `/dev/dxg` がある場合は、Codex サンドボックス設定、`nvidia-smi` を含む `/usr/lib/wsl/lib` の PATH、HPC SDK の環境変数を Agent の子プロセスへ渡します。登録先を一覧で開くだけでは、既存タスクの回復処理や移行処理を走らせません。

`POST /api/tasks` は最上位タスクと `dev` からの専用ブランチを作ります。`POST /api/tasks/<id>/children` は親ブランチから子の専用ブランチを作ります。各タスクの `task.json` は `parent_id`、`root_id`、`branch`、作業の状態、コミット ID を持ちます。GUI が管理するタスクの記録は `tasks/` に置き、Git から除外します。

新規タスクの実体フォルダは `YYYY-MM-DD_HHMM_作成時の名称_ID` とし、既存のID名フォルダは移動しません。日時は作成時の日本時間です。名前変更後も実体フォルダ名は固定します。`tasks/by-name/`（追加リポジトリは `tasks/<リポジトリID>/by-name/`）には実体フォルダへのシンボリックリンクを置き、旧プロジェクト記録には `projects/by-name/` を使います。リンクは作成時とサーバー起動時に補い、タスク削除時に取り除きます。API は内部IDで新旧の実体フォルダを引きます。

葉タスクの Agent 実装は、変更を専用 worktree に残し、レポート原稿を `tasks/<id>/report-draft.html` に作ります。GUI が成功した原稿を `reports/0001.html` 以降に保存します。通常の Agent 実行はコミットとマージを行いません。完了操作では、一回の Agent 実行で、確認済みの変更全体を一件以上のコミットに分け、子なら親へ、最上位なら `dev` へ `--no-ff` でマージし、作業ブランチと worktree を削除します。コミット済みで統合だけが残るタスクと子を持つ親では、マージと削除だけを依頼します。統合に失敗したコミット済みタスクも、未完了のうちは 02 のチャットで修正できます。新しい変更があれば次の完了操作でそれをコミットしてから統合します。Agent は統合先の作業場所で動き、統合先の Git 共通ディレクトリと worktree の置き場所に加えて、タスクの作業場所と統合先の作業場所そのものを書き込み可能なディレクトリとして渡します。サンドボックスは、起動した場所と、追加で許可したディレクトリ自身の worktree 管理領域（`.git/worktrees/<名前>`）にしか Git が書けず、許可した親フォルダの中にある別の worktree の管理領域は読み取り専用になります。作業場所を明示しないと、タスクのコミットが `Read-only file system` で失敗します。GUI は実行後に次を検証します。各コミットは `Task-Operation` 行を持ち、承認したファイルだけを変更し、最終ツリーが承認時の作業内容と一致すること。統合先の新しい HEAD は、親が「承認時の統合先 HEAD」と「タスクの最終コミット」のマージコミットで、`Task-Operation` 行を持つこと。競合がない場合は `git merge-tree --write-tree` の結果とツリーが一致すること。競合を解決した場合は、統合先からの変更ファイルがタスクの変更範囲に収まること。統合先の作業ツリーが空であること（統合前に空でなくても完了操作は始まり、Agent が統合先の状態を見て整えます）。Agent が作業ブランチを削除しなかった場合は GUI が削除し、会話にその旨を残します。サンドボックスでは `.git/worktrees` の管理情報を消せないことがあるため、検証後に GUI が `git worktree prune` を実行します。失敗時は、統合先にこのタスクの途中のマージが残っていれば GUI が `git merge --abort` で戻します。同じ統合先への完了実行は同時に一件だけです。

`begin_agent_completion` は統合先に未追跡ファイルや追跡中の未コミット変更があっても拒否せず、`operations["target_dirty"]`（`untracked` と `tracked`）に記録して、指示文に渡します。指示文は、未追跡ファイルを削除せず `<リポジトリ名>-worktrees/_completion-moved/<操作ID先頭8桁>/` へ退避すること、追跡中の変更・原因不明の状態・確実に解決できないコンフリクト・仮説を支持しない検証のままの統合は、何も変更せずに質問することを求めます。応答の JSON（[commit-response.schema.json](../.codex/skills/research-workflow/references/commit-response.schema.json)）は `message`・`needs_user_input`・`question` を持ちます。`needs_user_input` が真なら GUI は検証と完了記録を行わず、途中のマージを中止し、Agent がすでに作ったコミットは通常どおり検証して記録します。そして `task.json` の `completion_waiting`（`question`・`asked_at`・承認済みの `paths`）を保存し、質問を `assistant` の発言として `chat_result.json` に追記します。実行の状態は `needs_input` で、表示の段階は「質問あり」（次に動くのはユーザー）です。この間の 02 のチャットは通常の検証チャットではなく、回答として `AgentManager.resume_completion` に渡り、新しい承認確認をそのまま使って同じ完了を再開します（質問のあとに承認済みのパス以外の変更が増えていれば拒否します）。01 のチャット、画像・ファイルの添付、空の回答は拒否します。完了すると `completion_waiting` は消えます。実行ごとに、ユーザーの方針と、結果・作成したコミット・マージ・Agent の報告を 02 の `chat_result.json` に追記します。失敗または中断した場合は、CLI 出力の末尾と標準エラーを `tasks/<タスクID>/diagnostics/completion-<実行ID>.json` に保存します。子を持つ親は全子の完了と最新の総括が必要です。コミットする変更が無く、コミットも保留もない成功済みタスクの事前確認は `kind: "empty"` を返し、Agent を使わずに `complete_task` が完了します。作業ブランチが統合先の祖先であること（未統合のコミットが無いこと）を確認してから、作業場所とブランチを削除し、コミットのない完了として記録します（`legacy_completed` は立てません）。

既存の `projects/` は起動時に読み取り、所属タスクの `root_id` と各作業ブランチを整えます。元の記録は控えとして残し、新しい操作では参照しません。古い Git 操作記録の回復は移行より先に実行します。

## 画面と Agent

01 のチャットは計画と実装可否を更新します。「実装に進む」は任意の追加指示とともに実装・初回検証を開始します。02 のチャットは結果への質問、追加検証、計画内の修正と再検証を受け付けます。実装の実行はコードの変更が無くても、有効なレポートが保存されれば成功です（`changed_files` は空になります）。コードを変えない測定・検証の計画ではコードを変更しないよう指示し、コードを変える計画なのに変更が無い場合は `outcome` を `inconclusive`、`needs_followup` を真にして理由を返させます。コードの変更が無く、かつ結論が保留で追加の検証が必要と返った実行は失敗にします。実装が成功したタスクは、変更ファイルが無くても 02 のチャットを使えます。実行 ID ごとの状態は `/api/runs/<id>` で取得し、`/api/runs/<id>/cancel` で中断を要求します。実行の記録はサーバーのメモリにだけあるため、再起動後は見つかりません。画面は取得に失敗するとタスクの状態を確認し、サーバー側で実行中でなければ追跡を終えます。想定外の例外で実行が止まった場合も、サーバーは実行を失敗にしてタスクを解放します。途中のコード変更は残し、未完成のレポートを確定しません。

Agent の入力には会話の直近分、過去レポートの抜粋、最近の資料名を渡します。全文と原本は保存先を示し、必要なものだけ開くよう指示します。これにより、画像や過去レポートが増えても初期入力が無制限に膨らみません。画面の資料画像は遅延読み込みし、サーバーは大きな資料を順次配信します。

Claude Code の最終イベントでは `structured_output` を優先し、なければ `result` の JSON 文字列を読み込みます。有効な JSON オブジェクトが得られない場合は、原因と再実行前に変更を確認する案内をチャットに表示します。CLI 出力の末尾と標準エラーは `tasks/<タスクID>/diagnostics/claude-<実行ID>.json` に保存します。追加リポジトリのタスクでは `tasks/<リポジトリID>/<タスクID>/diagnostics/` に保存します。

01・02 のチャット画像は既存の資料 API で `evidence/` に保存し、`chat_plan.json` または `chat_result.json` の該当ユーザーメッセージにファイル名を記録します。送信時には同じタスクの画像か検証し、最大4枚・各10 MBを適用します。Codex CLI には現在の依頼に付いた画像を `--image` で渡します。Claude Code CLI には保存先を会話に示し、Read ツールで開くよう指示します。

画像以外のチャット添付は `app/attachments.py` が扱います。拡張子を問わず `POST /api/tasks/<id>/attachments?name=<元の名前>` で受け取り、`attachments/<8桁の16進>-<安全な名前><拡張子>` に保存します（各100 MB以下）。チャットの送信本文 `files` に保存名を最大10件まで指定でき、同じタスクの `attachments/` にあるか検証してからユーザーメッセージの `files` に記録します。Agent への依頼文には保存先の絶対パスを示し、形式に合う方法で読み、編集しないよう指示します。`GET /api/tasks/<id>/attachments/<name>` は `application/octet-stream` と `Content-Disposition: attachment` でダウンロードだけを返します。

Claude Code CLI は `claude --print --output-format stream-json --json-schema` で呼び出し、最終応答を Codex と同じ形式の JSON として扱います。モデルは `fable`・`opus`・`sonnet`・`haiku` の別名か、空欄の既定（CLI 設定）から選びます。表示名のバージョン（例: Opus 5.5）は、サーバーが初回に各別名で CLI を起動し、`init` イベントの解決済みモデル ID から作ります。結果はサーバーの再起動まで保持し、タスクには別名を保存するため新しいバージョンにも追従します。権限は Codex のサンドボックス設定に合わせます。`workspace-write` では Claude Code の Bash サンドボックスを必須にし、サンドボックス内のコマンドを自動許可します。ネットワークは `app/agent.py` の `CLAUDE_ALLOWED_DOMAINS` と、環境変数 `DEVELOP_AGENT_CLAUDE_ALLOWED_DOMAINS`（カンマ区切り）のドメインだけに接続できます。Linux では `bubblewrap` と `socat` が必要で、無い場合は実行せずに理由を表示します。`danger-full-access` では権限確認をすべて省略します。どちらもスキルの手順、タスク保存先、必要に応じて開発コードとコミット用の Git 共通ディレクトリを `--add-dir` で許可します。

## 計画・レポートの形式

新規タスクの計画は `plan.html`、レポートは `reports/NNNN.html` です。`plan.html` が無く `plan.md` がある既存タスクは `plan.md` を読み書きし、既存の `reports/*.md`・`result.md` は変換しません。タスク API は `plan_format`（`"html"` または `"markdown"`）と `plan_file`（`plan.html` など）を返し、各レポートは `format` を持ちます。どちらも保存せず、ファイル名から求めます。`plan_hash` は保存中の計画ファイルの本文から計算します。子タスク案の承認で作る初期計画も HTML です。

HTML の文書は `app/static/html-document.mjs` が表示します。`<script>`・`<meta>`・`<link>` を除き、`evidence/<filename>` と `../evidence/<filename>` を資料 API の URL に置き換え、`$...$`・`$$...$$` を KaTeX で描画し、基本スタイルと CSP（自サーバー以外の読み込みとスクリプトを禁止）を加えて `srcdoc` にします。iframe は `sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"` でスクリプトを無効にし、親画面から `contentDocument` の高さを読み取って枠の高さを合わせます。計画の要点（目的・確認したいこと）とレポートの題（結論）は、`<h1>`〜`<h3>` の見出し文言から次の `<h1>`/`<h2>` までの文字を抜き出します。

## ファイル参照

`app/file_browser.py` は読み取り専用のファイル参照を扱います。書き込み API はありません。共通の問い合わせは `repo`（リポジトリ ID）、`task`（任意）、`path`（ルートからの相対パス）です。

| API | 返す内容 |
| --- | --- |
| `GET /api/files` | ディレクトリ一覧（名前、種別、サイズ、更新日時、ディレクトリ優先・名前順、最大 2000 件）とルートの場所 |
| `GET /api/files/content` | ファイルの情報と本文。テキストは先頭 1 MB まで（超えると `truncated`）、画像は `kind: image`、NUL を含むか UTF-8 でないものは `kind: binary` |
| `GET /api/files/raw` | ファイルそのもの。`download=1` で添付扱い |

`task` を指定するとタスクのブランチの worktree（無ければリポジトリ本体）を、指定しないとリポジトリ本体を参照します。閲覧のために worktree は作りません。`..`・絶対パス・`.git` を含むパスと、シンボリックリンクで解決後にルート外や `.git` へ出るパスは拒否し、一覧にも出しません。`raw` は HTML を `text/plain` で返し、`X-Content-Type-Options: nosniff` と `Content-Security-Policy: sandbox` を付けて同一オリジンでスクリプトが動かないようにします。

画面側は `app/static/files.mjs` の `openFileBrowser({ repository, taskId, path })` で開きます（`window.openFileBrowser` からも呼べます）。`taskId` を省くとリポジトリ本体、`path` を渡すとそのファイルまでツリーを展開して表示します。ツリーはディレクトリを開いたときに読み込みます。テキストは行番号付き、Markdown は `markdown.mjs`、HTML はスクリプト無効の `sandbox` iframe（`srcdoc`）でプレビューします。HTML プレビューは相対パスの画像などを読み込みません。スタイルは `app/static/files.css` にあり、モジュール読み込み時に追加されます。

## ホームと定時実行

`GET /api/home` は登録済みの全リポジトリについて、タスクのカード情報（`display_status`・`plan_approved` など）、メモ、担当 Agent、日報の日付、最近の夜間記録を返します。そのほかの API は `repo` クエリで対象を選びます。

| API | 内容 |
| --- | --- |
| `POST /api/home/memos`・`PATCH`/`DELETE /api/home/memos/<id>` | メモ `{title, body, allow_agent}` の登録・編集・削除 |
| `PATCH /api/home/agent` | 担当 Agent `{provider, model}` の保存 |
| `GET /api/home/reports/<YYYY-MM-DD>` | 日報 HTML `{date, html}` |
| `POST`/`DELETE /api/tasks/<id>/plan-approval` | 計画の承認・取り消し。承認は `can_implement` のときだけ、実行中は不可 |

承認は `task.json` の `plan_approval: {plan_hash, approved_at, source}` に保存します（`source` は `user` か `memo`）。`read_task` は保存中の計画の hash と一致し、`can_implement` のときだけ派生値 `plan_approved` を真にします。夜間に自動実行した hash は `auto_run_plan_hash` に記録し、開始できなかった場合は元に戻します。

保存先は各プロジェクトのタスク記録ルート（`tasks/`、追加リポジトリは `tasks/<リポジトリID>/`）直下の `.home/` です。`task.json` を持たないため、タスク一覧とリンク作成は無視します。

| ファイル | 内容 |
| --- | --- |
| `memos.json` | メモの配列 `{id, title, body, allow_agent, task_id, created_at, updated_at}` |
| `agent.json` | 担当 Agent `{provider, model}` |
| `scheduler.json` | 最後に処理した回 `{nightly, daily_report}`（日本時間の ISO 8601） |
| `nightly/YYYY-MM-DD.json` | 夜間実行の記録 `{date, project, started_at, finished_at, entries}`。`entries` は `{kind, task_id, title, run, state, message, ...}` |
| `daily-reports/YYYY-MM-DD.html` | 対象日の日報 |

スケジューラは `server.py` の `main` が起動するスレッドで、30 秒ごとに各プロジェクトの直近の 0:00・6:00 の回と `scheduler.json` を比べ、未処理の回をプロジェクトごとのスレッドで順に実行します。逃した回が複数あっても直近の 1 回だけ実行します。`scheduler.json` が無い初回は、実行せずに直近の回を処理済みとして保存します。夜間処理は Agent の各 run の終了を `AgentManager.get_run` で待ってから次へ進みます。実装済みのタスクは `start_implementation` では前回の変更ファイルを引き継げないため、02 のチャット（`start_chat(phase="result")`）で追加実験を依頼します。

日報は 6:00 の回で、前日（日本時間 0:00–24:00）に `updated_at` が変わったタスクか、同じ夜（当日 0:00）の夜間記録がある場合だけ作ります。担当 Agent を Codex では `--sandbox read-only`、Claude Code では編集ツールを禁止して起動し、JSON 応答 `{summary, html}` の HTML から `<script>` を除いて保存します。画面は `sandbox` 属性付き iframe の `srcdoc` で表示します。判定ロジックは時刻を引数で受け取るため、`tests/test_home.py` で時刻を固定して確認できます。

## Markdown と数式

`app/static/markdown.mjs` は許可したリンクと画像だけを DOM（画面を構成する要素の木構造）に追加します。`../evidence/<filename>` は当該タスクの資料 API（画面からサーバーへデータを求める窓口）へ変換されます。数式は KaTeX（文字で数式を書く LaTeX 記法を画面に描くライブラリ）で描画します。文中の `$...$` と独立した `$$...$$` を扱い、HTML を直接解釈しません。KaTeX は `app/cli/package-lock.json` で固定し、`/vendor/katex/` から配信します。

## 開発と確認

Python は `uv sync --frozen` で `.venv` を用意し、有効化してからデバッグします。JavaScript の依存関係は `npm ci --prefix app/cli` で導入します。基本確認は次を実行します。

```bash
source .venv/bin/activate
python -m unittest discover -s tests -q
node --test tests/test_markdown.mjs tests/test_html_document.mjs
```

Git 操作を変更した場合は、隔離した一時リポジトリで子から親、親から `dev` への統合と移行のテストを確認してください。
