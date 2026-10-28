# app

ローカル Web UI、タスク保存、Git 連携、検証資料、CLI 接続の実装です。`repositories.py` が登録済みリポジトリごとのサービスを作り、`task_workflow.py` がタスクごとのブランチと完了操作を扱います。`record_index.py` は日時と名称から記録を探すリンクを作ります。`workflow.py` は旧プロジェクトの Git 操作を移行前に回復します。構成と保存形式は[開発・システム設計ガイド](../docs/development-guide.md)を参照してください。
