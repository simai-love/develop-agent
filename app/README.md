# app

ローカル Web UI、タスク保存、Git 連携、検証資料、CLI 接続の実装です。`repositories.py` が登録済みリポジトリごとのサービスを作り、`task_workflow.py` がタスクごとのブランチと完了操作を扱います。`storage.py` は新規タスクを日時と名称付きのフォルダに保存し、既存IDフォルダも読み取ります。`record_index.py` は記録を探すリンクを作ります。`workflow.py` は旧プロジェクトの Git 操作を移行前に回復します。`home.py`・`memos.py`・`plan_approval.py` はホーム画面のデータ、メモ、計画の承認を扱い、`scheduler.py` が `nightly.py`（0:00 の自動実行）と `daily_report.py`（6:00 の日報）を起動します。構成と保存形式は[開発・システム設計ガイド](../docs/development-guide.md)を参照してください。
