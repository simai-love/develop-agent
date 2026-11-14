# tests

ローカル CLI のモックを用いて、計画、検証、資料、中断、完了時の Agent によるコミット・マージ・ブランチ削除、その検証と再起動後の回復、履歴と診断保存を確認します。一時 Git リポジトリではタスク専用ブランチ、コミット・マージ、旧記録の移行と HTTP API を確認します。登録した別リポジトリの Git と、このアプリの中央タスク保存先を使う経路も確認します。実行は `uv run python -m unittest discover -s tests -v` です。

旧プロジェクト方式のテストは、移行前の Git 操作回復の互換性を確認するために残しています。

`test_agent_stream.py` はCLIイベントの変換、実行中の進捗取得、実行中断を確認します。

`test_response_schemas.py` はCodexの厳格なJSON応答形式で必要な必須項目を確認します。

`test_chat_images.py` はチャット画像と任意形式の添付ファイルの検証、安全な保存名、履歴保存、Codex・Claude Codeへの受け渡しを確認します。

`test_claude_command.py` はClaude Codeの権限・サンドボックス・追加ディレクトリの指定、モデル別名とバージョン表示、エラー応答の読み取りと形式外応答の診断保存を確認します。

`test_project_api.py` は `~/prog` 相当の候補検索APIと、登録済みリポジトリごとのタスク操作を確認します。

`test_documents.py` は新規タスクの HTML の計画・レポート保存と、`plan.md`・`reports/*.md` の旧形式互換を確認します。

`test_record_index.py` は日時・名称付き実体フォルダとリンクの作成、既存IDフォルダの互換性を確認します。

`test_home.py` は計画の承認と無効化、段階表示、夜間実行の対象選定と順次実行、逃した回の補完、日報の対象判定と読み取り専用の CLI 呼び出し、メモとホーム API を、時刻を固定して確認します。

`test_file_browser.py` はファイル参照のパス検査（`..`、シンボリックリンク、`.git` 除外）、一覧の並び、テキストの切り詰めとバイナリ判定、タスク worktree を含む API と安全な配信ヘッダーを確認します。

Markdown の表示と安全なリンク処理は `node tests/test_markdown.mjs`、HTML の計画・レポートの表示用変換（資料 URL、スクリプト除去、見出しからの要点抽出）は `node tests/test_html_document.mjs` で確認します。
