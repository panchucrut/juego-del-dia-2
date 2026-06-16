# ⚽ Juego del Día

App para apostar marcadores en la fase de grupos. Cada día se juega por el "día":
adivinas los marcadores de todos los partidos, sumas puntos y peleas por el campeonato.

## Reglas (resumen)

**Puntos por partido** (te llevas el más alto, no se suman):
- 🟢 Solo el ganador → **1 pto**
- 🔵 La diferencia de goles → **2 ptos**
- 🟣 El marcador exacto → **4 ptos**

**Puntos del día** (según tu lugar en el ranking del día):
- 🥇 1.º → **+2** · 🥈 2.º → **+1** · 🙂 medio → **0** · 😬 penúltimo → **−1** · 💀 último → **−2**
- Empates: se reparten en partes iguales (siempre suma cero).

Otras reglas: máximo **15 jugadores**, entras solo con tu **nombre**, puedes
**cambiar tu apuesta hasta justo antes del partido** (luego se cierra 🔒).
La explicación didáctica completa está dentro de la app, en **Reglas**.

## Correr en tu PC (Windows / PowerShell)

```powershell
cd C:\Users\panch\juego-del-dia
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Abre http://localhost:5000

- Jugadores: entran con su nombre.
- Organizador: abajo de todo dice "organizador" → PIN por defecto **1234**
  (cámbialo con la variable `ADMIN_PIN`). Ahí cargas partidos y resultados.

## Ponerlo online (gratis) con Render

1. Sube esta carpeta a un repo de GitHub.
2. En [render.com](https://render.com) → **New** → **Blueprint** → elige el repo.
   Render lee `render.yaml`, crea la web **y una base de datos Postgres gratis**
   (los datos persisten entre reinicios).
3. Cuando pregunte por `ADMIN_PIN`, pon el PIN que quieras.
4. Listo: te da una URL tipo `https://juego-del-dia.xxxx.onrender.com`. Compártela.

> Nota: el plan free "duerme" tras un rato sin uso; la primera visita puede tardar
> ~30 s en despertar. Para 15 personas es más que suficiente.

### Alternativa rápida sin deploy
Corre `python app.py` en tu PC y comparte tu PC con internet con
[ngrok](https://ngrok.com): `ngrok http 5000` te da una URL pública temporal.

## Configuración (variables de entorno)
- `ADMIN_PIN` — PIN del organizador (default `1234`).
- `SECRET_KEY` — clave de sesiones (ponla en producción).
- `DATABASE_URL` — si existe, usa Postgres; si no, SQLite local (`juego.db`).

## Zona horaria
Las horas de los partidos se manejan en **hora de Chile** (`America/Santiago`).
