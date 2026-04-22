# Prompt de Auditoría — Trading System

Copiá **todo lo que está debajo de la línea `---`** y pegalo como prompt en Claude Code parado en `/Users/charlie/Desktop/trading-system`. No edites el texto.

Este prompt es **SOLO LECTURA**. Claude Code no debe modificar archivos, ni correr ningún bot real, ni mandar emails, ni hacer órdenes a Alpaca. Solo leer, ejecutar scripts de lectura local (sqlite queries, `--dry-run`) y reportar.

---

## Objetivo

Sos un auditor técnico. Leé todo el repo `/Users/charlie/Desktop/trading-system`
y producí un **informe estructurado** que me permita entender qué tengo armado
y dónde puede haber problemas. **No toques ningún archivo.** No corras los bots
en modo real. No mandes emails. No envíes órdenes a Alpaca. Podés:

- Leer archivos (`Read`, `Glob`, `Grep`).
- Ejecutar queries de solo lectura contra `trading_paper.db` y `mrev_paper.db`.
- Correr cualquier script con `--dry-run` si existe el flag.
- Correr `python3 -m py_compile` y tests del proyecto (si están en `tests/`).

Si algo no está claro o es ambiguo, **preguntalo al final en la sección
"Necesito confirmar con el usuario"**. No asumas.

## Informe a producir

Usá el formato exacto de secciones de abajo. Dentro de cada sección, citá
archivos con `file:line` concreto (no frases vagas tipo "en algún lado del
código"). Donde detectes un riesgo o bug, marcalo con `[ALERTA]`. Donde haya
hardcode o valores mágicos, marcalo con `[MAGIC NUMBER]`. Donde algo sea un
trade-off razonable pero que yo debería saber, marcalo con `[NOTA]`.

### 1. Arquitectura general

- ¿Cuántos bots hay? ¿Qué estrategia implementa cada uno?
- ¿Cómo se coordinan (si es que lo hacen) con el capital de la cuenta
  Alpaca compartida? ¿Hay alguna posibilidad de que los dos bots pisen
  la misma posición? ¿Cómo se previene?
- Diagrama textual simple: entrada de datos → scanner → señales → ejecución
  → DB → email.
- Archivos principales, qué hace cada uno en una línea.

### 2. Lógica de entrada (compras)

Para cada bot:

- Condiciones exactas que se evalúan para abrir posición. Listado numerado
  (C1, C2, ... o X1, X2, ... según la nomenclatura del código).
- ¿Qué indicadores se calculan y cómo? (`atr14`, `rsi14`, `ema21`, `ema50`,
  `sma_20`, `bb_lower`, etc.)
- Cómo se decide el size de la posición (fórmula exacta + cap).
- Cómo se decide el stop-loss inicial.
- Cómo se rankean candidatos cuando hay más señales que slots.
- Máximo de posiciones simultáneas y cómo se enforza.

### 3. Lógica de salida (ventas)

Para cada bot:

- **Partial take-profit**: qué stage, a qué porcentaje, qué fracción se vende.
  Citá las env vars relevantes y sus defaults.
- **Take-profit final** (si existe, ej. E7 en RFTM): fórmula y condición.
- **Stop-loss**: hard stop fijo y/o dinámico; stop se mueve a breakeven en
  algún momento? ¿cuándo exactamente?
- **Trailing stop**: fases, multiplicadores de ATR, umbral de activación.
- **Time stop**: en qué unidades (días, bars, horas) y umbral.
- Orden de evaluación de las reglas en `check_exit`.
- [ALERTA] si encontrás una regla mencionada en el email pero NO ejecutada en
  `check_exit`, o viceversa.

### 4. Estado `partial_tp_taken` (stage counter)

- ¿Qué significa cada valor (0, 1, 2, ...)?
- ¿Dónde se escribe? ¿dónde se lee? (archivos + líneas)
- ¿Existe transición 2 → cerrado por E7?
- ¿Qué pasa si el qty remanente queda < 2 (no se puede partir más)?
- ¿Hay posiciones legacy con el viejo significado binario (flag 0/1)? ¿se
  manejan bien?

### 5. Base de datos

- Schema de `positions` (RFTM) y `mrev_positions` (MREV). Columnas + tipos.
- ¿Qué tablas auxiliares hay? (`runs`, `mrev_runs`, `mrev_signals`,
  `mrev_snapshots`, `mrev_hourly_log`, `mrev_email_log`, `market_data`, etc.)
- Estado actual en vivo:
  ```sql
  SELECT symbol, qty, entry_price, stop_loss, initial_qty, partial_tp_taken,
         highest_since_entry, opened_at
  FROM positions WHERE status='open';
  ```
  y el equivalente en `mrev_positions`. Mostralo en tabla en el reporte.
- [ALERTA] si detectás posiciones cripto mezcladas en `positions` (RFTM), o
  ETFs en `mrev_positions`, o `initial_qty` NULL, o `partial_tp_taken > 2`.
- ¿Hay índices en las columnas que se usan para filtrar (`status`, `symbol`)?
  Si no, [NOTA] con impacto esperado.

### 6. Integración con Alpaca

- Endpoints usados (GET /account, GET /positions, POST /orders, GET /orders,
  GET bars, etc.).
- Cómo se hace la sincronización Alpaca ↔ DB local. ¿Quién es la fuente de
  verdad? ¿qué se arregla en cada corrida?
- Bracket orders: ¿se usan? ¿qué campos llevan? ¿se cancelan cuando el bot
  mueve el stop (ej. a breakeven)?
- ¿Cómo se maneja el caso "Alpaca rechazó la orden"?
- ¿Cómo se maneja el caso "filled_qty < ordered_qty" (partial fill)?
- ¿Se chequea buying power antes de enviar la orden? ¿qué pasa si cambia
  entre la señal y el submit?
- [ALERTA] si hay algún path donde se modifica el estado local ANTES de
  confirmar el fill con Alpaca.

### 7. Emails y notificaciones

Para cada bot:

- ¿Qué email manda? ¿Cuándo (ventanas horarias)?
- HTML: qué secciones incluye. Cita las secciones por nombre.
- ¿El "Take Profit $X" que aparece dibujado corresponde a un exit real del
  código? (Este es un bug histórico — verificá que ya esté corregido con
  `E7_take_profit`.)
- ¿Hay un email por evento cuando dispara TP1/TP2/E7? ¿O solo el resumen?
- ¿Qué SMTP se usa? ¿Credenciales de dónde salen?
- Preview HTML generado: mostrame los nombres de los archivos
  `email_preview*.html` / `mrev_email_preview*.html` que existen hoy y a qué
  caso corresponden.
- [ALERTA] si el email del MREV (1h) está mostrando datos del RFTM o viceversa.

### 8. Configuración

- Listá **todas** las env vars que el código lee (`os.environ.get(...)`).
  Para cada una: archivo, línea, default, qué controla, si está documentada
  en un comentario.
- [ALERTA] si hay defaults que parecen inseguros o no documentados (ej.
  `MAX_DRAWDOWN = 0.20` hardcoded sin env var).
- Env vars mencionadas pero sin uso efectivo (dead config).
- `.env.paper`: qué tipo de keys espera (no imprimas los valores).

### 9. Scheduling / automatización

- ¿Cómo corre el bot en producción? ¿launchd, cron, GitHub Actions,
  manual?
- Archivos relevantes (`.plist`, `Makefile`, `.github/workflows/*`,
  `paper_trade.sh`, `run_rftm_bot.sh`, `setup_autorun.sh`).
- ¿A qué horas corre cada bot? ¿Hay solapamiento o potencial doble ejecución?

### 10. Logging y observabilidad

- Dónde se escriben los logs (archivos, stdout, ambos).
- ¿Rotan? ¿crecen sin límite?
- Niveles de log que existen.
- [ALERTA] si detectás `print()` de secrets o keys.

### 11. Manejo de errores

- ¿Qué pasa si Alpaca está caído?
- ¿Qué pasa si no hay datos de mercado para un símbolo?
- ¿Qué pasa si `.env.paper` está vacío?
- ¿Qué pasa si la DB está bloqueada (otro proceso escribiendo)?
- ¿Hay reintentos? ¿backoff?
- [ALERTA] cualquier `except Exception: pass` silencioso.

### 12. Risk management

- Max drawdown / kill switch: ¿cómo, dónde, qué hace?
- Max positions y max position % del portfolio.
- Risk per trade (RISK_PCT): ¿se respeta cuando el ATR es muy chico o
  muy grande?
- Correlación entre posiciones: ¿se considera? (ej. QQQ + SPY + IWM + XLK
  están altamente correlacionados).
- [NOTA] límites que en la práctica no se pueden cumplir (ej. buying_power
  insuficiente para el target size).

### 13. Tests

- ¿Hay tests? En `tests/` listá los archivos.
- Corré `python3 run_tests.py` si existe, o `pytest tests/` — reportá
  resultado. **Solo tests unitarios**, nada que mande requests reales.
- [ALERTA] tests skipeados o que siempre pasan sin verificar nada.
- Coverage cualitativo: qué funciones están testeadas y cuáles no.

### 14. Código sospechoso / deuda técnica

- `TODO`, `FIXME`, `XXX` comments: listalos con archivo:línea.
- Funciones marcadas como `REMOVED` / `deprecated`: siguen en el código?
- Variables muertas, imports no usados.
- Archivos que parecen scratchpads (`_*.py`, `test_*.py` sueltos, archivos
  en raíz que no están en `apps/` o `packages/`).
- [ALERTA] código duplicado largo entre RFTM y MREV.

### 15. Seguridad

- `.env*` en `.gitignore`?
- `.env.paper` commiteado por error?
- Logs que imprimen valores de env sensibles?
- Permisos de archivos (`.env.paper` debería ser `600`).
- Inputs de usuario que llegan a queries SQL sin parametrizar.

### 16. Diferencias RFTM vs MREV

Tabla comparativa (en el reporte) con estas filas:

| Aspecto               | RFTM             | MREV             |
|-----------------------|------------------|------------------|
| Timeframe             |                  |                  |
| Universo              |                  |                  |
| Capital               |                  |                  |
| Risk per trade        |                  |                  |
| Max positions         |                  |                  |
| Entry                 |                  |                  |
| Stop loss             |                  |                  |
| Take profit final     |                  |                  |
| Trailing              |                  |                  |
| Partial TPs           |                  |                  |
| Time stop             |                  |                  |
| Email window          |                  |                  |
| DB                    |                  |                  |

### 17. Features recientes — verificá que estén y funcionen

El usuario pidió recientemente estas features. Para cada una, decime si
está implementada correctamente. Citá archivo:línea de la implementación.

1. Partial TP en dos etapas (5% vende 50%; 7.5% vende otro 25%).
   - Env vars `PARTIAL_TP1_PCT`, `PARTIAL_TP1_SELL_RATIO`, `PARTIAL_TP2_PCT`,
     `PARTIAL_TP2_SELL_RATIO` con defaults `0.05 / 0.50 / 0.075 / 0.50`.
   - Backward compat con `PARTIAL_TP_PCT` / `PARTIAL_TP_SELL_RATIO`.
   - Aplica en **ambos** bots.

2. `E7_take_profit` en `check_exit` de RFTM: cuando `stage >= 2` y
   `close >= entry + 2 × (entry − stop_loss)`.

3. `sync_with_alpaca` en ambos bots que inserta posiciones faltantes con
   `partial_tp_taken=0`, `initial_qty=qty`.

4. Detección correcta de cripto (no solo por `/`, también prefijos
   `BTC/ETH/SOL/AVAX/DOGE/LINK/DOT/ADA/MATIC/XRP` + sufijo `USD/USDT/USDC`).

5. `seed_missing_positions.py` existe y (a) migra cripto atrapada en
   `trading_paper.db`, (b) siembra GLD, SLV, AVAX/USD, etc. con stage=0.

6. Traducciones en HTML del email para `partial_tp1_*` y `partial_tp2_*`.

Para cada una marcá: ✅ implementado OK · ⚠️ parcial o con gap · ❌ no está.

### 18. Features pendientes — NO las implementes, solo verificá que no estén

El usuario pidió estas en otro prompt. **No las implementes acá.** Solo
decime si ya fueron implementadas o siguen pendientes:

1. Stop-loss sube a breakeven cuando dispara TP1.
2. Email inmediato por cada evento de TP (TP1/TP2/E7), no solo en el
   resumen diario.
3. En el email diario RFTM, por posición, línea "faltan X% para el próximo
   stage".
4. El email del bot 1h (MREV) habla de MREV, no de RFTM.

Si alguna ya está, indicá archivo:línea. Si no, marcalo pendiente.

### 19. Resumen ejecutivo (último, pero el más importante)

En no más de 15 bullets:

- 3 cosas que están **bien hechas** y no tocaría.
- 3 cosas que están **medio armadas** y necesitan terminarse.
- 3 cosas que son **claramente un problema** y hay que atacar primero.
- 3 cosas que son **unknowns / supuestos riesgosos** que hay que
  confirmar con datos reales.
- 3 cosas que son **riesgos potenciales de dinero** (no de código), por
  ejemplo: "si el ATR de un ETF explota, el size podría ser enorme
  porque el cap por posición es 25%".

### 20. Necesito confirmar con el usuario

Lista de preguntas concretas que te quedaron. Una por línea, numeradas.

## Formato del entregable

- Un único mensaje final en markdown con todas las secciones.
- Si el resumen es muy largo, podés crear también un archivo
  `AUDIT_REPORT_YYYYMMDD.md` en `/Users/charlie/Desktop/trading-system/` y
  linkearlo, pero **ese archivo solo contiene el informe — no toques
  nada más**.
- No hagas commits.
- No mandes emails.
- No llames a Alpaca con POST.
- Al final del informe, imprimí en la terminal:
  `AUDIT OK — informe generado (lectura nada más).`

Fin del prompt. Empezá por la sección 1 y andá en orden.
