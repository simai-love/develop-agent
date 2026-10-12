# Develop Agent

研究開発の目的、仮説、実装方針、検証結果を、プロジェクトと階層化したタスクに残すローカル GUI です。計画の相談から実装・検証結果の確認、完了判断までを画面で進めます。

## 環境構築と起動

Python 3.12 以降、[uv](https://docs.astral.sh/uv/)、Node.js と npm、Git を用意してください。新しいプロジェクトを作るには、このリポジトリに `dev` ブランチが必要です。初回の Codex CLI 取得にはネットワーク接続が必要です。

リポジトリのルートで次を実行します。Python の依存関係は `pyproject.toml` と `uv.lock` に基づき `uv` で同期し、アプリ専用の Codex CLI は `app/cli/package-lock.json` に基づき npm で導入します。

```bash
uv sync --frozen
npm ci --prefix app/cli
bash scripts/run.sh
```

`scripts/run.sh` は `.venv` を有効化して Python 環境を再同期し、Codex CLI が未導入なら npm で導入してサーバーを起動します。ブラウザで `http://127.0.0.1:8765` を開いてください。Agent のチャットと実装には、選んだ CLI の認証とモデルの利用権限が必要です。Claude Code CLI を選ぶ場合は別途導入・認証してください。

## 文書案内

このリポジトリの文書は次のとおりです。用途に合わせて本文へ直接進めます。

| 読む人・目的 | 文書 | 分かること |
| --- | --- | --- |
| 初めて画面を使う人 | [利用ガイド](docs/user-guide.md) | 作成、01 計画、実装、02 検証、完了、記録確認の操作順 |
| 実装経路と更新先を調べる開発者 | [開発・システム設計ガイド](docs/development-guide.md) | 画面、API、保存、Git、変更先と検証方法 |
| Agent の作業規則を調べる人 | [研究開発スキルの説明](.codex/skills/research-workflow/README.md)・[手順本文](.codex/skills/research-workflow/SKILL.md) | 01・実装・02 と GUI の担当 |

利用ガイドには操作に対応する画面例を掲載しています。画像の再現条件は[画面例の説明](docs/screenshots/README.md)で確認できます。

## フォルダ案内

| 読む人・目的 | フォルダ | 中身 |
| --- | --- | --- |
| 画面や API を直す開発者 | [app/](app/README.md) | GUI、保存処理、Agent との接続 |
| ブラウザ画面を直す開発者 | [app/static/](app/static/README.md) | HTML、CSS、JavaScript |
| ブランチ操作を調べる開発者 | [app/git/](app/git/README.md) | ブランチ、worktree、コミット、マージ |
| CLI の実行環境を調べる開発者 | [app/cli/](app/cli/README.md) | アプリ専用 Codex CLI の npm 管理 |
| 起動処理を調べる人 | [scripts/](scripts/README.md) | 環境同期とサーバー起動のスクリプト |
| 動作確認をする開発者 | [tests/](tests/README.md) | Python と JavaScript のテスト |
| Agent の動作を調べる人 | [.codex/](.codex/README.md) | GUI と Agent の連携方針 |
| Agent の作業手順を調べる人 | [.codex/skills/](.codex/skills/README.md) | 利用可能なスキル |
| 研究開発タスクの指示を調べる人 | [.codex/skills/research-workflow/](.codex/skills/research-workflow/README.md) | 計画・検証の共通手順 |
| Agent の表示設定を調べる人 | [.codex/skills/research-workflow/agents/](.codex/skills/research-workflow/agents/README.md) | Agent の表示設定 |
| 画面別の指示を調べる人 | [.codex/skills/research-workflow/references/](.codex/skills/research-workflow/references/README.md) | 01・02 の指示と応答形式 |
| 計画・結果の書式を調べる人 | [.codex/skills/research-workflow/assets/](.codex/skills/research-workflow/assets/README.md) | Markdown テンプレート |
| 文書を直す人 | [docs/](docs/README.md) | 利用ガイドと設計資料の原稿 |
| 画面例を更新する人 | [docs/screenshots/](docs/screenshots/README.md) | サンプル画面と再現条件 |

実行中のプロジェクトとタスクの記録は、リポジトリ直下の `projects/` と `tasks/` に保存され、Git 管理から除外されます。各ファイルの用途は[利用ガイドの「保存された記録を確認する」](docs/user-guide.md#保存された記録を確認する)を参照してください。
