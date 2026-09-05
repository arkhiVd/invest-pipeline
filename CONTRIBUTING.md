# Contributing

Create a branch and keep the change focused. Add deterministic tests before
changing calculation code.

Use synthetic instruments and values. Names such as `ACME`, `NOVA`, and `ZEAL`
are fixtures, not recommendations. Mock all network clients. Never attach a
broker export, database, statement, application screenshot, or production log.

Run the local gate from `AGENTS.md`. Pull requests that change methodology must
update `SPEC.md` and explain boundary behavior with hand-worked examples.
Broker-write code is out of scope.

This project is educational software, not financial advice. Avoid wording that
presents a signal, score, or backtest as a recommendation.
