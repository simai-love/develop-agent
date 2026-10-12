# Develop Agent

研究開発の目的、仮説、実装方針、検証結果をプロジェクトと階層化したタスクで整理するローカルGUIです。

GUIの実装は `app/`、Agent向けの指示は `AGENTS.md` と `.codex/`、設計資料は `docs/` に置きます。実行時に作成する `tasks/` は個人の作業データであり、Git管理から除外します。

## 起動

Python 3.12以降、`uv`、Node.js、npm、Gitを想定しています。Python環境は `pyproject.toml` と `uv.lock` を用いて `uv` で管理します。初回起動時にアプリ専用のCodex CLIをインストールするため、ネットワーク接続が必要です。Codex CLIへのログインも必要です。Claude Code CLIは別途インストール済みの場合に選択できます。

```bash
bash scripts/run.sh
```

ブラウザで `http://127.0.0.1:8765` を開きます。起動スクリプトが `uv` で `.venv` を同期・有効化します。

GUIでは、開発項目の作成、親子関係の表示、計画と検証レポートのMarkdown保存、状態管理、ユーザーによる完了操作ができます。計画・検証のチャットと実装ボタンは、選択したローカルCLIに接続します。計画チャットでは、Agentが選択式の質問を出し、`tasks/<id>/plan.md` を直接更新します。保存した計画をAgentが実装可能と判定した場合に実装ボタンが有効になります。実装開始時は確認ダイアログを表示し、コード変更に続けて検証します。結果が得られるたびに `tasks/<id>/reports/` に新しいレポートを保存し、02画面のトグルから閲覧できます。02のチャットから追加検証やコード修正・再検証を依頼できます。検証資料の画像・動画・データは画面で閲覧できます。実装と検証記録を確認した後、タスクの完了はユーザーが決めます。

計画・検証レポート・チャットは見出し、太字、箇条書きなどを整えて表示します。計画のMarkdown本文は「Markdownを編集」から変更できます。01の選択式質問には質問ごとに自由記述欄があり、選択肢または文章で回答できます。

Agentチャット欄の下端を上下にドラッグすると高さを調整できます。選択した高さはブラウザに保存され、次回も使われます。下端にキーボードでフォーカスした場合は上下矢印キーでも調整できます。

Agentの実行中はチャット欄の直上に、計画作成中・実装中・追加検証中の案内を表示します。実装中に画面を更新した場合も、保存された状態を確認して表示を更新します。左の一覧と詳細画面には「ユーザー待ち」「実行中」「完了済み」などと次の操作を表示します。

Codex CLIが見つからない場合は、チャットと実装ボタンは利用できません。プロジェクト作成時に `dev` からブランチを作り、子タスクの検証後に完了ボタンでコミットします。子を持つタスクは総括後に親ブランチへマージし、全タスクが終わればプロジェクトを `dev` にマージできます。Agentが分割を提案した場合、承認後に子タスクを作成します。グラフ表示は後続の実装対象です。

既存の最上位開発項目は起動時にプロジェクトへ移行します。元の作業ツリーに未コミット変更がある場合は、プロジェクト画面に確認案内を表示します。変更をGitで整理した後、「整理を確認」を押すと移行先ブランチを更新できます。
移行前に検証済みで新しい差分がないタスクは、確認後に過去の完了記録として扱い、新しいコミットを作りません。

開発項目は詳細画面と左の一覧から子タスクを追加・削除できます。左のプロジェクト一覧からはプロジェクト名の変更と削除もできます。名前を変更してもブランチ名は変わりません。削除は確認を経て行い、子タスクも一緒に消えます。計画・チャット・レポート・資料は復元できません。Git履歴や未コミット変更があるプロジェクトは削除できません。Agent実行中のタスクを含む場合も削除できません。Codexの初期モデルは `gpt-6-sol` です。モデル欄にはローカルCLIの `model/list` が提示する候補を表示し、変更できます。候補の利用可否は契約・ワークスペース設定によります。Claude Codeはモデル欄を表示せず、CLI側の設定を使用します。

開発項目の名前は詳細画面の「名前を変更」から編集できます。変更すると左の一覧にも反映されます。

## フォルダ案内

- [`.codex/`](.codex/README.md): Web UI の Agent スキルと接続方針
- [`app/`](app/README.md): GUI とローカル API
- [`app/git/`](app/git/README.md): Git ブランチと worktree の操作
- [`app/cli/`](app/cli/README.md): Codex CLI の実行環境
- [`app/static/`](app/static/README.md): ブラウザ画面
- [`docs/`](docs/README.md): 詳細設計
- [`scripts/`](scripts/README.md): 起動手順
- [`tests/`](tests/README.md): 動作確認
- [`.codex/skills/`](.codex/skills/README.md): スキル一覧
- [`.codex/skills/research-workflow/`](.codex/skills/research-workflow/README.md): 計画・検証スキル
- [`.codex/skills/research-workflow/agents/`](.codex/skills/research-workflow/agents/README.md): Codex 表示設定
- [`.codex/skills/research-workflow/references/`](.codex/skills/research-workflow/references/README.md): 画面別の Agent 指示
- [`.codex/skills/research-workflow/assets/`](.codex/skills/research-workflow/assets/README.md): Markdown テンプレート

保存形式と機能の境界は [docs/architecture.md](docs/architecture.md) を参照してください。
