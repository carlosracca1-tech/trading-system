# state/db — snapshots persistentes de runtime

Esta branch NO es código. Es la fuente de verdad de los archivos de
estado que generan los bots:

- `trading_paper.db` — RFTM SQLite DB (positions, runs, snapshots).
- `mrev_paper.db` — MREV SQLite DB.
- `logs/trade_events_rftm.jsonl` — eventos RFTM (KAIZEN).
- `logs/trade_events_mrev.jsonl` — eventos MREV (KAIZEN).
- `logs/kaizen_missed_moves.jsonl` — rebotes perdidos por cooldown.

Cada workflow al terminar pushea sus archivos acá vía
`scripts/state_db_push.sh` (force push, una sola commit).

Backups rotativos: cada commit conserva los 7 snapshots anteriores
como `{file}.bak-{N}` (N=1..7). Si la DB de un run se corrompe,
recuperable desde la rotación.

Pull desde local: `make sync-db` (corre `scripts/sync_db.sh`).
