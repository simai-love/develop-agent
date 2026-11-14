# static

ローカル GUI の HTML、CSS、JavaScript です。タスク一覧、親子タスクの状態、画像や任意のファイルを添付できるAgentチャット、分割提案と実行中コマンド、検証資料、数式、HTML の計画・レポート（`html-document.mjs` がスクリプト無効の iframe で表示）、変更ファイルの差分、完了時のコミット・マージ確認を表示します。`files.mjs` と `files.css` は読み取り専用のファイルブラウザで、`openFileBrowser({ repository, taskId, path })` を他の画面からも呼べます。`home.mjs` と `home.css` は全プロジェクトのタスクカード、メモ、担当 Agent、日報・夜間記録を表示し、カードとプロジェクトをドラッグで並べ替えられる（順番はブラウザに保存）ホーム画面と、タスクの段階表示の色です。画面の動作は[開発・システム設計ガイド](../../docs/development-guide.md)を参照してください。
