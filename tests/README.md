# tests

ローカル CLI のモックを用いて、計画、検証、資料、中断を確認します。一時 Git リポジトリではタスク専用ブランチ、コミット・マージ、旧記録の移行と HTTP API を確認します。登録した別リポジトリの Git と、このアプリの中央タスク保存先を使う経路も確認します。実行は `uv run python -m unittest discover -s tests -v` です。

旧プロジェクト方式のテストは、移行前の Git 操作回復の互換性を確認するために残しています。

`test_agent_stream.py` はCLIイベントの変換、実行中の進捗取得、実行中断を確認します。

`test_response_schemas.py` はCodexの厳格なJSON応答形式で必要な必須項目を確認します。

Markdown の表示と安全なリンク処理は `node tests/test_markdown.mjs` で確認します。
