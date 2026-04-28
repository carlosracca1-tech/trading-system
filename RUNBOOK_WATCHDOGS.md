# RUNBOOK — Watchdogs de Exits

**Ámbito:** `rftm_watchdog.py` y `mrev_watchdog.py`. Dos procesos
dedicados a cuidar los exits de las posiciones abiertas — los bots
entry ya no ejecutan exits.

---

## Arquitectura rápida

```
┌──────────────────────┐       ┌─────────────────────────┐
│ standalone_paper_    │       │ standalone_mrev_        │
│ trader.py            │       │ trader.py               │
│ MODE=entry_only      │       │ MODE=entry_only         │
│ cron: 13:35 UTC L-V  │       │ cron: :05 cada hora     │
└────────┬─────────────┘       └──────────┬──────────────┘
         │ INSERT posición                │ INSERT posición
         ▼                                ▼
   ┌─────────────────────────────────────────┐
   │          Alpaca Paper account           │
   │           (cuenta compartida)           │
   └─────────────┬────────────┬──────────────┘
                 │            │
                 │            │
   ┌─────────────┴────┐  ┌────┴────────────────┐
   │ rftm_watchdog.py │  │ mrev_watchdog.py    │
   │ */5 13-20 * * 1-5│  │ */5 * * * *         │
   │ (horario NYSE)   │  │ (24/7)              │
   └──────────────────┘  └─────────────────────┘
```

- Entry bots: MODE=entry_only → solo `check_entry` + buy.
- Watchdogs: partial TP1/TP2 → stop loss → trailing → time stop.
- Fuente de verdad de qty: Alpaca. Fuente de estado de estrategia
  (stage, stop, highest_since_entry): DB local.

---

## Cómo diagnosticar si un watchdog se cayó

1. Ir a **GitHub → Actions → RFTM/MREV Watchdog**.
2. Mirar los últimos 10 runs: ¿hubo fallas? ¿cuándo fue el último
   run exitoso?
3. Si hubo una falla, abrir el log del run y buscar:
   - `DB health check failed` → DB corrupta o falta columna. Ver
     sección "DB drift" más abajo.
   - `No Alpaca keys` → secrets no configurados.
   - `integrity_check failed` → SQLite roto; restaurar de cache anterior.
   - `Alpaca HTTP 4xx` → token expirado o request malformado.
4. Si el workflow entero no corrió (no aparece en la lista), probable
   que el schedule esté deshabilitado. Ver **.github/workflows/*.yml**
   — el schedule está comentado por defecto hasta validación manual.

---

## Cómo forzar un run manual

1. GitHub → Actions → RFTM/MREV Watchdog (Exits) → `Run workflow`.
2. `dry_run`: **true** (default) para modo diagnóstico; **false** para
   que mande órdenes reales a Alpaca paper.
3. `force_run` (solo RFTM): **true** para ejecutar incluso con mercado
   cerrado.
4. `Run workflow`.

Alternativa local:

```bash
export ALPACA_API_KEY=... ALPACA_SECRET_KEY=... DRY_RUN=true
python3 rftm_watchdog.py
python3 mrev_watchdog.py
```

---

## Cómo interpretar un exit loguado

Buscar líneas como:

```
  [ok]  SPY: partial_tp1 — sell 5 @ $105.30
  [ok]  DOGE/USD: EXIT (stop_loss (close=0.089 ≤ stop=0.090)) — sell 1000 @ $0.089
```

Campos:
- **partial_tp{1,2}**: parcial. TP1 → sube stop a entry (breakeven).
  TP2 → no toca el stop.
- **EXIT (razón)**: cierre total.
  - `E3_stop_loss` / `stop_loss (...)`: tocó el stop.
  - `E5_trailing_aggressive` / `trailing_stop (...)`: trailing gatilló.
  - `E5_breakeven_stop`: precio bajó del entry tras TP1.
  - `E6_time_stop` / `time_stop (Xh)`: posición vieja.
  - `E7_take_profit`: RFTM-only; TP final a 2:1 R:R tras TP2.
  - `take_profit (close≥sma+1.5atr=...)`: MREV TP final.

Tras un EXIT con `stop_loss`/`trailing_stop`/`time_stop`, MREV **graba
cooldown** (6h default). El entry bot rechazará re-entradas con el
mensaje `SKIP BTC/USD: cooldown (4.2h remaining)`.

---

## Procedimiento si la cache de DB se pierde

GitHub Actions usa `actions/cache` para persistir `mrev_paper.db` /
`trading_paper.db` entre runs. Si la cache se evicciona o la key cambia:

1. El próximo run restaurará de `restore-keys` (prefijo). Si no hay
   restore, el bot creará schema limpio (estado reset).
2. Si perdiste estado de estrategia: el primer run de
   `sync_with_alpaca` repobla posiciones abiertas con `stage=0`. Esto
   **resetea progreso de TPs** — una posición que ya había hecho TP1
   volverá a evaluar TP1 si el precio sigue >+5%.
3. Para recuperar stage real: mirar fills históricos en Alpaca (GET
   `/v2/orders?status=filled`) y correr `mark_partial_tp_done.py`
   manualmente sobre los símbolos afectados (o `seed_missing_positions.py`).

---

## Qué hacer si hay drift masivo entre DB y Alpaca

1. Correr `scripts/ops/preflight.py`. Reporta:
   - `in Alpaca but not in any DB`: posiciones huérfanas. Normalmente
     `sync_with_alpaca` las reclama en el próximo run.
   - `in DB but not in Alpaca`: ya se cerraron fuera del bot. Normalmente
     `sync_with_alpaca` las cierra en el próximo run (razón
     `synced_from_alpaca`).
2. Si el drift es grande (>5 símbolos), correr:
   ```bash
   python3 seed_missing_positions.py --dry-run
   ```
   y revisar el plan antes de ejecutarlo sin `--dry-run`.
3. En último caso: `mark_partial_tp_done.py --symbol XXX --stage N`.

---

## DB drift (schema)

Si `assert_db_health` falla con `missing columns [...]`:

1. Agregar la columna via `ALTER TABLE ... ADD COLUMN ... DEFAULT ...`
   a mano en una consola:
   ```bash
   sqlite3 mrev_paper.db
   ALTER TABLE mrev_positions ADD COLUMN initial_qty REAL;
   .quit
   ```
2. Actualizar `_migrate_db` / bloque de ALTERs en `get_db` del bot
   correspondiente para que la migración sea idempotente.
3. Actualizar `_db_health.RFTM_REQUIRED_COLUMNS` / `MREV_REQUIRED_COLUMNS`
   si la nueva columna debe ser obligatoria.
4. Commit + re-run.

---

## Env vars clave (watchdog)

| Var | Default | Descripción |
|-----|---------|-------------|
| `DRY_RUN` | `true` | Si es `true`, no envía órdenes. El workflow setea explícitamente. |
| `FORCE_RUN` | `false` | RFTM-only: correr aunque el mercado esté cerrado. |
| `WATCHDOG_FILL_TIMEOUT_S` | `10` | Segundos de espera para que un order llene antes de cancelarlo. |
| `WATCHDOG_BARS_LOOKBACK` | `40` | RFTM: barras diarias para computar ATR14. |
| `MREV_COOLDOWN_HOURS` | `6` | Cooldown tras exit negativo en MREV. |

---

## Checklist antes de habilitar cron

- [ ] `python3 -m pytest tests/ -v` → todos pasan (incluye test_watchdog).
- [ ] `python3 scripts/ops/preflight.py` → exit 0.
- [ ] `DRY_RUN=true` manual run en GitHub → log limpio, sin acciones
      inesperadas.
- [ ] Revisar positions en Alpaca: ninguna sorpresa.
- [ ] Descomentar `schedule:` en los yml de watchdog.
- [ ] Merge a main.
- [ ] Monitorear primer run automático.

---

## Historial de operaciones

### 2026-04-27 — Op 2.4: liquidación de TPs atrasados (post-fix DRY_RUN)

**Contexto:** desde commit `22fa0d2` (23-abr) hasta `35c5cbc` (27-abr,
PR #2), los watchdogs corrieron en cron con `DRY_RUN=true` por bug en
el fallback del workflow. Ningún partial TP ni exit se vendió en
producción durante esos 4 días.

**Acción:** dispar manual de ambos watchdogs con `dry_run=false` para
liquidar los TPs/exits que quedaron parados.

**Comandos disparados:**
- GitHub UI → Actions → "RFTM Watchdog (Exits)" → Run workflow:
  `dry_run=false`, `force_run=false`. Run ID: <pegá>
- GitHub UI → Actions → "MREV Watchdog (Exits)" → Run workflow:
  `dry_run=false`. Run ID: <pegá>

**Resultado:**
- TP1 disparados: <lista> — total realizado: $<X>
- TP2 disparados: <lista> — total realizado: $<X>
- Cierres: <lista> — total realizado: $<X>
- **Total liquidado:** $<X>

**Pre-Op:** se aplicó `scripts/ops/reconcile_position.py --all --apply`
para alinear DB local con Alpaca (cascada del 22-04 documentada en
AUDITORIA_PRE_GOLIVE.md). Cripto cerradas: AVAX/USD, DOGE/USD,
LINK/USD, SOL/USD. RFTM ajustadas: SPY (10→5), IWM (24→12),
QQQ (11→3), XLE (cerrada).