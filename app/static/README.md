# static

ローカル GUI の HTML、CSS、JavaScript です。タスク一覧、親子タスクの状態、画像を添付できるAgentチャット、分割提案と実行中コマンド、検証資料、数式、変更ファイルの差分、完了時のコミット・マージ確認を表示します。`files.mjs` と `files.css` は読み取り専用のファイルブラウザで、`openFileBrowser({ repository, taskId, path })` を他の画面からも呼べます。画面の動作は[開発・システム設計ガイド](../../docs/development-guide.md)を参照してください。
