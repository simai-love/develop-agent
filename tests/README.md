# tests

ローカル CLI のモックを用いて、計画の直接保存、実装後の検証記録、追加検証、検証資料の保存を確認します。一時 Git リポジトリではプロジェクト作成、親子ブランチ、コミット・マージ、移行と HTTP API を確認します。実行は `uv run python -m unittest discover -s tests -v` です。

複数のプロジェクト直下タスクについても、APIからの作成と同一ブランチでの順次コミットを確認します。

`test_agent_stream.py` はCLIイベントの変換と実行中の進捗取得を確認します。

`test_response_schemas.py` はCodexの厳格なJSON応答形式で必要な必須項目を確認します。

Markdown の表示と安全なリンク処理は `node tests/test_markdown.mjs` で確認します。
