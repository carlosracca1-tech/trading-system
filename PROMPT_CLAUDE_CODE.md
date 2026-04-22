# Prompt para Claude Code — Completar features del trading system

Copiá **todo lo que está debajo de la línea `---`** y pegalo como prompt inicial en Claude Code dentro de `/Users/charlie/Desktop/trading-system`. No cambies el texto.

---

## Contexto del repo

Estás trabajando en `/Users/charlie/Desktop/trading-system`. Es un sistema de
paper-trading en Alpaca con dos bots independientes:

- **RFTM (diario)** → `standalone_paper_trader.py` — opera ETFs, corre 1×/día.
- **MREV (1 hora)** → `standalone_mrev_trader.py` — opera cripto + algunos ETFs
  seleccionados, corre cada hora.

Ambos usan Alpaca Paper (keys en `.env.paper`) y guardan estado en SQLite:

- `trading_paper.db` → posiciones RFTM (tabla `positions`)
- `mrev_paper.db`   → posiciones MREV (tabla `mrev_positions`)

La columna `partial_tp_taken` NO es un booleano — es un **stage counter**:

- `0` = ninguna parcial ejecutada
- `1` = TP1 disparado (vendió 50% al +5%)
- `2` = TP2 disparado (vendió 50% del remanente = 25% del total al +7.5%)
  — queda el 25% final corriendo hasta E7 / trailing / breakeven / time stop

## Estado actual — **NO REHAGAS LO SIGUIENTE**, ya está implementado

En una sesión previa se agregó:

1. Partial TP en 2 etapas a 5% y 7.5% con env vars
   `PARTIAL_TP1_PCT` / `PARTIAL_TP1_SELL_RATIO` /
   `PARTIAL_TP2_PCT` / `PARTIAL_TP2_SELL_RATIO`.
   Las viejas `PARTIAL_TP_PCT` / `PARTIAL_TP_SELL_RATIO` siguen funcionando
   como alias de la etapa 1 (backward compat — mantenelo).
2. En `check_exit` del RFTM: nueva regla `E7_take_profit`. Cuando
   `stage >= 2` y `close >= entry + 2 × (entry − stop_loss)` → vende todo.
   Es el **mismo nivel** que el email dibuja como "Take Profit" — antes era
   cosmético, ahora es real.
3. `sync_with_alpaca` de RFTM y MREV reclama automáticamente cualquier
   posición de Alpaca que falte en la DB local, con `partial_tp_taken=0`
   e `initial_qty=qty_actual`.
4. Detección correcta de cripto en ambas direcciones (lo que empieza por
   `BTC/ETH/SOL/AVAX/DOGE/LINK/DOT/ADA/MATIC/XRP` y termina en
   `USD/USDT/USDC` va a MREV; lo demás a RFTM).
5. `seed_missing_positions.py` que ya siembra GLD, SLV, AVAXUSD, etc. y
   migra cripto atrapada en la DB equivocada.
6. Traducciones en email para `partial_tp1_*` y `partial_tp2_*`.

**NO duplicas, NO refactorees**, NO vuelvas a escribir nada de esto.

## Lo que falta — implementar con cuidado

### Feature 1 · Stop-loss sube a breakeven cuando dispara TP1

Cuando `partial_tp_taken` pasa de 0 → 1, actualizá `stop_loss` al valor de
entrada (`entry_price`), así la mitad restante nunca puede volver a pérdida.

- Aplicá en **ambos bots** (RFTM y MREV).
- Regla: el stop nuevo se setea como `max(stop_loss_actual, entry_price)`.
  **Nunca bajes el stop**, solo se sube.
- Lugar: exactamente en el bloque `UPDATE ... SET qty=?, partial_tp_taken=?...`
  que ya existe post-fill.
- Alpaca tiene un bracket-stop server-side creado en la compra. No toques el
  bracket — lo dejás como safety-net (peor caso sale al stop original).
  Bastante con que `stop_loss` en la DB local se mueva al breakeven; el bot
  usa ese valor en `check_exit` E3 para detectar el breakeven en runtime.
- Dejá un `info()` log que diga:
  `"E3 raised to breakeven for SYM: ${old_stop:.2f} → ${new_stop:.2f}"`.

Caso borde: si `partial_tp_taken` ya era 1 antes (posición legacy del sistema
viejo que vendió al 3%), dejala como está. No metas breakeven retroactivo.

### Feature 2 · Email inmediato cuando dispara TP1, TP2 o E7

Además del resumen diario, cuando se ejecuta un evento de stage (0→1, 1→2,
o 2→cerrado por E7), mandá un email **en ese mismo run**.

Reglas del email:

- **Uno por evento**, no uno por run con lista.
- **Breve**: máximo 8 líneas útiles. HTML simple, sin tablas grandes.
- Muestra valores concretos:
  - Símbolo y qué stage disparó (TP1 / TP2 / TP FINAL).
  - Precio de entrada, precio de venta, cantidad vendida, $ realizados.
  - Qty restante y stage nuevo.
  - **Próximo target** (si queda posición): precio exacto y % que falta.
    Si ya se cerró (E7), poné "Posición cerrada completamente".
- Subject: `[TP1] QQQ +5.0% · vendí 5 @ $635.25` (ejemplo). Conciso.
- Respetá `dry_run`: en dry-run no enviar, solo loguear.
- Reusá el helper SMTP que ya exista en el archivo (no crees un cliente nuevo).
- Si el SMTP falla, `warn()` y seguí. Nunca abortes el trade por un email.

**Importante**: UN email por evento. No mandes uno por TP y después otro por
"stage changed" — es redundante.

### Feature 3 · En el email diario RFTM, mostrar "cuánto falta al próximo stage"

En la sección **"Lo que tengo en cartera"** de `standalone_paper_trader.py`,
abajo de los 3 cuadrados (Stop Loss / Precio actual / Take Profit), agregá
una línea compacta por posición tipo:

> Stage 1 · próximo: TP2 a **$99.28** (faltan **6.0%**)

Cálculo del próximo target según `partial_tp_taken`:

- `stage == 0` → TP1: `target = entry × (1 + PARTIAL_TP1_PCT)`
- `stage == 1` → TP2: `target = entry × (1 + PARTIAL_TP2_PCT)`
- `stage == 2` → TP final: `target = entry + 2 × (entry − stop_loss)`

`delta_pct = (target − current) / current × 100`. Si `delta_pct < 0`, poné
"ya superado — dispara en la próxima corrida".

Se muestra para cada posición, con estilo discreto (gris claro, font 11px).

### Feature 4 · Arreglar email del bot 1h (MREV)

Hoy `standalone_mrev_trader.py` manda un email que habla de métricas de RFTM
(o mezcla mal las dos). Tiene que ser **espejo del email de RFTM pero con
datos de MREV**:

- Header: equity de MREV (no el total de Alpaca) y % vs `MREV_CAPITAL`.
- Sección "Lo que tengo en cartera": posiciones de `mrev_paper.db`.
  Para cada una:
  - cuadrados SL / precio / TP (usá el TP dinámico de MREV: `SMA20 + 1.5×ATR`
    en vez del 2:1 R:R que usa RFTM)
  - línea "Stage X · próximo: TPY a $Z (faltan W%)" igual que Feature 3,
    con los mismos 3 triggers (TP1 al 5%, TP2 al 7.5%, E7 es el TP dinámico
    de X1 de MREV).
- Sección "Actividad últimas 24h": `get_last_24h_activity(conn, run_id)` que
  ya existe en el archivo.
- Timing: el bot corre cada hora. Manteneé la ventana de envío ya existente
  (`get_email_window` / `should_send_email`). No mandes un email cada hora.

Si hay helpers HTML reutilizables de RFTM (estilos, builders de cuadrados),
movelos a un módulo compartido `_email_helpers.py` e importalos desde ambos.
Si eso es mucho scope, copiá la función y marcalo con TODO.

### Feature 5 — eliminada (duplicada con la 2)

No mandes un email extra de "stage changed" además del de TP. Son el mismo
evento.

## Restricciones y cuidados — NO BREAK ANYTHING

1. **Idempotencia**: todo lo que escribas tiene que ser re-ejecutable sin
   efectos dobles (no duplicar filas, no doble-email).
2. **Schema DB**: solo `ALTER TABLE ... ADD COLUMN` envuelto en try/except.
   No renombres, no dropees.
3. **Env vars legacy**: `PARTIAL_TP_PCT` / `PARTIAL_TP_SELL_RATIO` siguen
   siendo alias de etapa 1. No los borres.
4. **`ETF_UNIVERSE`, `ALL_SYMBOLS`, `CRYPTO_SYMBOLS`**: NO los modifiques.
   Ni agregues ni saques símbolos.
5. **`.env.paper`** tiene credenciales. No imprimir, no commitear, no leer
   para loguear.
6. **Dry-run**: en dry-run **ningún email** se envía ni ninguna orden real
   se manda. Chequealo antes de cada send y cada submit.
7. **Cambios mínimos**. No refactorees archivos enteros. Edits quirúrgicos.
   Si algo pide refactor grande, dejá un TODO y avisá.
8. **Comentarios**: si agregás env vars nuevas (ej. para SMTP extra),
   documentalas en un bloque al tope del archivo correspondiente.
9. **Logs**: usá los helpers `ok` / `info` / `warn` / `err` ya existentes.
   No agregues un logger nuevo.
10. **Test ritual después de cada feature**:

    ```bash
    python3 -m py_compile standalone_paper_trader.py \
                          standalone_mrev_trader.py \
                          seed_missing_positions.py
    ```

    Si alguno falla, arreglalo antes de seguir.

11. **Commits**: uno por feature, mensaje claro:
    - `feat(rftm,mrev): raise stop to breakeven after TP1`
    - `feat(email): send individual email on TP1/TP2/E7 fires`
    - `feat(email): show next-stage distance in daily summary`
    - `fix(mrev): email now reports MREV data, not RFTM`

## Verificación final obligatoria

Antes de dar por terminado:

1. Los tres archivos compilan.
2. Simulaciones inline (podés hacer un script temporal en `/tmp`):
   - Posición con stage=0, precio sube a +5% → dispara TP1, stop sube a
     breakeven, se genera email con "próximo: TP2 a $X".
   - Posición con stage=1, precio sube a +7.5% → dispara TP2, email con
     "próximo: TP final a $X".
   - Posición con stage=2, precio sube a +10% → dispara E7, email con
     "posición cerrada".
3. Abrí los HTML generados del email diario RFTM y del email 1h MREV:
   - RFTM muestra "faltan X% para TP2/TP final" en cada posición.
   - MREV habla de MREV (no de RFTM) y muestra el mismo detalle.
4. `python3 seed_missing_positions.py --dry-run` corre sin error.
5. Un backtest rápido de `python3 analyze_trades.py` (si existe) no revienta.

## Entregable final

Imprimí en la terminal un resumen con:

- Archivos tocados y rangos de líneas.
- Env vars nuevas (si las hay) con sus defaults.
- Cómo forzar el envío de un email de TP desde un REPL para testear.
- Qué NO se hizo y por qué (si dejaste algún TODO).

Fin del prompt. Empezá por la Feature 1 y no saltes el orden.
