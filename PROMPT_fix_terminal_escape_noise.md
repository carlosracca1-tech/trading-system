# Prompt para Claude Code — Arreglar ruido de escape sequences en terminal

> Pegar tal cual en una sesión nueva de Claude Code. Esta sesión NO necesita estar abierta sobre `trading-system` — el problema es del entorno del usuario (shell + terminal + Claude Code), no del repo.

---

## Contrato

Modo agente autónomo. **No me preguntes paso a paso.** Diagnosticá, arreglá, verificá, y reportá al final. Si lo único que tenés para decirme es "encontré X, ¿procedo?", la respuesta es sí.

Solo frená y preguntame si:

1. El fix requiere borrar o reescribir completamente un dotfile mío (`~/.zshrc`, `~/.bashrc`, `~/.config/...`). Backups + edits selectivos los hacés solo, sin preguntar.
2. El fix requiere reinstalar el shell, el terminal, o Claude Code.
3. El fix requiere `sudo` para algo no-trivial.
4. Necesitás que ejecute algo en una terminal limpia que vos no podés abrir desde tu sandbox.

Cualquier otra cosa — editar dotfiles con backup, exportar env vars, tocar settings de iTerm/Terminal vía `defaults`, agregar guards condicionales, instalar paquetes con brew sin sudo — la decidís y la ejecutás vos.

## Síntoma

Cuando uso Claude Code (o pego cosas en Cowork/chat), aparecen secuencias de escape ANSI crudas tipeadas como input, ejemplos:

```
^[]11;rgb:0000/5e9c/6471^G
^[[59;30R
^[[59;38R
```

Lo que pasó:

- `\e]11;rgb:...\a` (OSC 11) = el terminal contestó una consulta de "¿cuál es tu color de fondo?".
- `\e[<row>;<col>R` (CSI ... R) = el terminal contestó una consulta de "¿dónde está el cursor?".

Algún programa preguntó, nadie leyó la respuesta, y la respuesta cayó como input. Aparece en ráfagas largas — o sea no es un single-shot, es polling continuo de algo (prompt del shell, plugin, app TUI mal portada).

Sistema: macOS. Tenés que detectar shell y terminal vos.

## Plan de ataque

### 1. Recolectar contexto del entorno

```bash
echo "shell: $SHELL"
echo "term: $TERM"
echo "term_program: $TERM_PROGRAM"
echo "term_program_version: $TERM_PROGRAM_VERSION"
echo "lc_terminal: $LC_TERMINAL"
echo "colorterm: $COLORTERM"
ls -la ~/.zshrc ~/.zshenv ~/.zprofile ~/.bashrc ~/.bash_profile ~/.profile 2>/dev/null
ls -la ~/.config/starship.toml ~/.p10k.zsh 2>/dev/null
which starship 2>/dev/null && starship --version
ps -p $$ -o comm=
```

Identificá:

- Terminal real (iTerm2, Terminal.app, Warp, Ghostty, Alacritty, kitty, VS Code integrated).
- Shell (zsh, bash, fish).
- Prompt framework si existe (oh-my-zsh, powerlevel10k, starship, pure, spaceship).

### 2. Hipótesis ordenadas por probabilidad

Probalas en este orden, descartá rápido y seguí:

1. **Prompt framework con auto-detect de color de fondo en cada redraw.** Powerlevel10k, starship, y algunos temas de oh-my-zsh consultan OSC 11 en cada prompt. Si el terminal responde más rápido de lo que el shell consume, las sobras se filtran. Buscalo en:
   - `~/.zshrc` → líneas con `STARSHIP_`, `POWERLEVEL9K_`, `p10k`, `theme`.
   - `~/.config/starship.toml` → sección `[palette]` o detección automática.
   - `~/.p10k.zsh` → bloque de detección de fondo claro/oscuro.
2. **Bracketed paste mode roto** entre el shell y la app TUI (Claude Code). zsh con `zle_bracketed_paste` activo + Claude Code que también lo activa = doble wrap, escape sequences se duplican.
3. **Focus reporting (`\e[?1004h`)** activo. Cada vez que cambiás de ventana, el terminal manda `\e[I` o `\e[O`, que algunas apps no consumen.
4. **ModifyOtherKeys / xterm extended keyboard mode** activo en iTerm pero la app no lo soporta.
5. **`TERM` mal seteado** — `TERM=xterm-256color` en un terminal que no es xterm puro genera negociaciones raras.
6. **Plugin de zsh** (zsh-syntax-highlighting, zsh-autosuggestions) en versión vieja que dispara queries por carácter tipeado.

### 3. Diagnóstico activo

Reproducí el bug controladamente para aislar la fuente:

```bash
# Test 1: shell desnudo, ¿sigue pasando?
env -i TERM=xterm HOME=$HOME PATH=/usr/bin:/bin /bin/zsh -f
# Tipear, pegar, mover ventana. Si NO pasa acá → es el rc del usuario.
exit

# Test 2: Claude Code con TERM básico
TERM=xterm claude
# Si NO pasa → es feature avanzada que tu terminal y Claude negocian mal.

# Test 3: ¿Qué dotfile lo dispara? Bisect
mv ~/.zshrc ~/.zshrc.bak
zsh
# ¿Pasa? Si no, el culpable estaba en .zshrc. Restaurá y andá comentando bloques.
```

### 4. Fix

Aplicá la cura más quirúrgica que apunte al culpable. Reglas:

- **Backup primero, edit después.** `cp ~/.zshrc ~/.zshrc.bak.$(date +%Y%m%d-%H%M%S)` antes de tocar cualquier dotfile.
- **Edits idempotentes**. No appendees líneas que ya están. Usá `grep -q` antes de `>>`.
- **Comentá lo que sacás, no lo borres.** Dejá `# disabled by claude-code 2026-05-03 (causa: OSC 11 leak): <linea original>` así puedo revertir si rompo otra cosa.
- **No toques shells de root, no toques nada en `/etc/`.**

Recetas comunes según el culpable identificado:

**Si es starship con auto-detect:**

```bash
mkdir -p ~/.config
# Forzar paleta sin auto-detect
cat >> ~/.config/starship.toml <<'EOF'

# disabled background color detection — fix OSC 11 leak
[palette]
EOF
```

O exportá en `~/.zshenv`:

```bash
echo 'export STARSHIP_LOG=error' >> ~/.zshenv
```

**Si es powerlevel10k:**

Editá `~/.p10k.zsh` y desactivá `POWERLEVEL9K_BACKGROUND_DETECTION` o seteá `POWERLEVEL9K_TERM_SHELL_INTEGRATION=false`. Backup primero.

**Si es bracketed paste duplicado en zsh:**

```bash
# en ~/.zshrc, al final
unset zle_bracketed_paste
```

**Si es focus reporting:**

```bash
# en ~/.zshrc
printf '\e[?1004l'  # disable focus reporting al abrir shell
```

**Si es `TERM` mal seteado:**

```bash
# en ~/.zshenv (no .zshrc — tiene que cargar antes)
export TERM=xterm-256color  # estándar para iTerm/Terminal.app
```

**Si es feature avanzada de iTerm que Claude Code no maneja:**

```bash
# Settings de iTerm2 vía defaults (sin tocar UI)
defaults write com.googlecode.iterm2 "AllowPasteBracketing" -bool false
# Reiniciar iTerm para que tome efecto
```

### 5. Verificación

Después de cada fix, antes de declarar victoria:

```bash
# Cerrá completamente la terminal (no solo la pestaña — la app entera)
# Abrí una nueva
echo "test 1: prompt vacío"
# tipear enter 5 veces, ver si aparecen escape sequences
echo "test 2: paste"
# pegar un bloque multilinea
echo "test 3: claude code"
claude
# tipear, pegar, cambiar de ventana, volver
```

Si sale limpio en los tres tests por al menos 2 minutos de uso normal, está arreglado.

### 6. Reporte final

Mandame esto y nada más:

```
## Entorno detectado
- Shell: <zsh/bash/...>
- Terminal: <iTerm2 3.5/Terminal.app/Warp/...>
- Prompt framework: <powerlevel10k/starship/none/...>
- TERM: <valor>

## Diagnóstico
- Causa raíz: <una línea>
- Cómo lo aislé: <test que lo confirmó>

## Fix aplicado
- Archivos tocados: <lista con paths absolutos>
- Backups creados: <lista>
- Líneas agregadas/comentadas: <diff corto>
- Settings de app modificados: <si tocaste defaults o GUI>

## Verificación
- Test 1 (prompt vacío): <pass/fail>
- Test 2 (paste): <pass/fail>
- Test 3 (Claude Code 2min): <pass/fail>

## Reversión
- Para revertir: <comando o pasos exactos>

## Pendientes para Charlie
- <solo lo que requiere mi mano, ej. reiniciar iTerm o cambiar un setting GUI que no se puede via defaults>
```

Sin postamble.
