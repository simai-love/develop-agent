# app

ローカル Web UI、タスク保存、Git 連携、検証資料、CLI 接続の実装です。`repositories.py` が登録済みリポジトリごとのサービスを作り、`task_workflow.py` がタスクごとのブランチと完了操作を扱います。`storage.py` は新規タスクを日時と名称付きのフォルダに保存し、既存IDフォルダも読み取ります。計画とレポートは HTML（既存タスクは Markdown も可）です。`record_index.py` は記録を探すリンクを作ります。`file_browser.py` はプロジェクト内ファイルを読み取り専用で一覧・表示します。`workflow.py` は旧プロジェクトの Git 操作を移行前に回復します。構成と保存形式は[開発・システム設計ガイド](../docs/development-guide.md)を参照してください。
