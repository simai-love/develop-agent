# app

ローカル Web UI、プロジェクト・タスク保存、Git 連携、検証資料、CLI 接続の実装です。`workflow.py` がプロジェクト直下の複数タスクと承認された操作を順序づけ、`git/` がブランチ・worktree・コミット・マージを扱います。`agent_stream.py` は CLI の実行イベントを画面用の作業履歴に変換します。構成と保存形式は [開発・システム設計ガイド](../docs/development-guide.md) を参照してください。
