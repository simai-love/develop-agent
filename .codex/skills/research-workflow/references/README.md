# 画面別の指示

[01-plan.md](01-plan.md) は計画・実装可否・子タスクの分割案、[02-validation.md](02-validation.md) は検証・修正・親の総括を指示します。機械可読な返答形式は、計画用の [plan-response.schema.json](plan-response.schema.json) と検証・総括用の [validation-response.schema.json](validation-response.schema.json) に定義します。選択式質問は 01 でのみ使います。両スキーマの全項目を必須にし、該当しない提案やコミット文は空配列・空文字列で返します。
