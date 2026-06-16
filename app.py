import os
import json
import urllib.request
import unicodedata
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, abort, g, jsonify)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# ----------------------------------------------------------------------------
# Configuracion
# ----------------------------------------------------------------------------
TZ = ZoneInfo("America/Santiago")   # hora de Chile
MAX_PLAYERS = 15

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambia-esto-en-produccion")

db_url = os.environ.get("DATABASE_URL", "sqlite:///juego.db")
if db_url.startswith("postgres://"):                 # compat Render/Heroku
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

ADMIN_PIN = os.environ.get("ADMIN_PIN", "1234")      # PIN del organizador

db = SQLAlchemy(app)


# ----------------------------------------------------------------------------
# Modelos
# ----------------------------------------------------------------------------
def now_local():
    return datetime.now(TZ).replace(tzinfo=None)


class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(40), unique=True, nullable=False)
    pin_hash = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=now_local)


class Match(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    home_team = db.Column(db.String(40), nullable=False)
    away_team = db.Column(db.String(40), nullable=False)
    kickoff = db.Column(db.DateTime, nullable=False)      # hora Chile (naive)
    match_date = db.Column(db.Date, nullable=False)       # el "dia"
    home_score = db.Column(db.Integer)
    away_score = db.Column(db.Integer)
    finished = db.Column(db.Boolean, default=False)


class Bet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=False)
    match_id = db.Column(db.Integer, db.ForeignKey("match.id"), nullable=False)
    home_pred = db.Column(db.Integer, nullable=False)
    away_pred = db.Column(db.Integer, nullable=False)
    updated_at = db.Column(db.DateTime, default=now_local)
    __table_args__ = (db.UniqueConstraint("player_id", "match_id"),)


class Setting(db.Model):
    key = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.String(200))


with app.app_context():
    db.create_all()


# ----------------------------------------------------------------------------
# Logica de puntos
# ----------------------------------------------------------------------------
def sign(x):
    return (x > 0) - (x < 0)


def bet_points(hp, ap, hs, as_):
    """Puntos de una apuesta vs el resultado real.
       4 = marcador exacto | 2 = misma diferencia | 1 = solo ganador | 0 = nada
       Predicción no ingresada cuenta como 0-0."""
    if hs is None or as_ is None:
        return 0
    if hp is None:
        hp = 0
    if ap is None:
        ap = 0
    if hp == hs and ap == as_:
        return 4
    if hp - ap == hs - as_:          # misma diferencia (incluye acertar empate)
        return 2
    if sign(hp - ap) == sign(hs - as_):
        return 1
    return 0


def day_awards(scores):
    """Reparte los puntos del dia (+2 / +1 / -1 / -2) segun el ranking.
       Empates -> promedio de los premios de las posiciones que ocupan.
       Siempre suma cero (lo que ganan unos lo pierden otros)."""
    n = len(scores)
    if n == 0:
        return {}
    reward = [0.0] * n
    reward[0] += 2                   # 1er lugar
    if n >= 2:
        reward[1] += 1               # 2do lugar
        reward[n - 2] += -1          # penultimo
    reward[n - 1] += -2              # ultimo

    ordered = sorted(scores.items(), key=lambda x: -x[1])
    awards = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        avg = sum(reward[i:j + 1]) / (j - i + 1)
        for k in range(i, j + 1):
            awards[ordered[k][0]] = avg
        i = j + 1
    return awards


def day_ranking(fecha):
    """Devuelve (scores, awards, matches, finished) para un dia.
       Incluye a TODOS los jugadores inscritos; sin apuesta cuenta 0-0."""
    matches = Match.query.filter_by(match_date=fecha).order_by(Match.kickoff).all()
    ids = [m.id for m in matches]
    finished = {m.id: m for m in matches if m.finished}
    bet_map = {}
    if ids:
        for b in Bet.query.filter(Bet.match_id.in_(ids)).all():
            bet_map[(b.player_id, b.match_id)] = b
    scores = {}
    for p in Player.query.all():
        total = 0
        for mid, m in finished.items():
            b = bet_map.get((p.id, mid))
            hp, ap = (b.home_pred, b.away_pred) if b else (0, 0)
            total += bet_points(hp, ap, m.home_score, m.away_score)
        scores[p.id] = total
    awards = day_awards(scores) if scores else {}
    return scores, awards, matches, finished


def overall_ranking():
    """Suma de premios de campeonato por jugador (solo dias con algo jugado)."""
    totals = {}
    for f in fechas_disponibles():
        scores, awards, matches, finished = day_ranking(f)
        if not finished:
            continue
        for pid, a in awards.items():
            totals.setdefault(pid, {"award": 0.0, "bet": 0})
            totals[pid]["award"] += a
        for pid, s in scores.items():
            totals.setdefault(pid, {"award": 0.0, "bet": 0})
            totals[pid]["bet"] += s
    return totals


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def is_locked(m):
    return now_local() >= m.kickoff


def fechas_disponibles():
    rows = (db.session.query(Match.match_date)
            .distinct().order_by(Match.match_date).all())
    return [r[0] for r in rows]


def fecha_default():
    fs = fechas_disponibles()
    if not fs:
        return None
    hoy = now_local().date()
    futuras = [f for f in fs if f >= hoy]
    return futuras[0] if futuras else fs[-1]


def get_setting(key, default=None):
    s = db.session.get(Setting, key)
    return s.value if s else default


def set_setting(key, value):
    s = db.session.get(Setting, key)
    if s:
        s.value = value
    else:
        db.session.add(Setting(key=key, value=value))
    db.session.commit()


def registration_open():
    return get_setting("registration_open", "1") == "1"


# Colores por jugador (uno distinto para cada uno) ---------------------------
PALETTE = ['#ef4444', '#f97316', '#f59e0b', '#eab308', '#84cc16', '#22c55e',
           '#10b981', '#14b8a6', '#06b6d4', '#3b82f6', '#6366f1', '#8b5cf6',
           '#a855f7', '#d946ef', '#ec4899']


def player_colors():
    # color estable por jugador (basado en su id, no en su posición)
    return {p.id: PALETTE[(p.id - 1) % len(PALETTE)]
            for p in Player.query.all()}


# Grupo de cada equipo (fase de grupos Mundial 2026) -------------------------
TEAM_GROUP = {
    'México': 'A', 'Sudáfrica': 'A', 'Corea del Sur': 'A', 'Rep. Checa': 'A',
    'Canadá': 'B', 'Bosnia': 'B', 'Qatar': 'B', 'Suiza': 'B',
    'Brasil': 'C', 'Marruecos': 'C', 'Haití': 'C', 'Escocia': 'C',
    'Estados Unidos': 'D', 'Paraguay': 'D', 'Australia': 'D', 'Turquía': 'D',
    'Alemania': 'E', 'Curazao': 'E', 'Costa de Marfil': 'E', 'Ecuador': 'E',
    'Países Bajos': 'F', 'Japón': 'F', 'Suecia': 'F', 'Túnez': 'F',
    'Bélgica': 'G', 'Egipto': 'G', 'Irán': 'G', 'Nueva Zelanda': 'G',
    'España': 'H', 'Cabo Verde': 'H', 'Arabia Saudita': 'H', 'Uruguay': 'H',
    'Francia': 'I', 'Senegal': 'I', 'Irak': 'I', 'Noruega': 'I',
    'Argentina': 'J', 'Argelia': 'J', 'Austria': 'J', 'Jordania': 'J',
    'Portugal': 'K', 'R.D. Congo': 'K', 'Uzbekistán': 'K', 'Colombia': 'K',
    'Inglaterra': 'L', 'Croacia': 'L', 'Ghana': 'L', 'Panamá': 'L',
}


def _standings(teams, matches, score_fn):
    """Tabla de posiciones. score_fn(m) -> (gl_local, gl_visita) o None para omitir.
       3 pts gana, 1 empate. Orden: pts, dif. gol, goles a favor, nombre."""
    tab = {t: {"team": t, "pj": 0, "pts": 0, "gf": 0, "gc": 0} for t in teams}
    for m in matches:
        sc = score_fn(m)
        if sc is None:
            continue
        hs, as_ = sc
        h, a = m.home_team, m.away_team
        if h not in tab or a not in tab:
            continue
        tab[h]["pj"] += 1
        tab[a]["pj"] += 1
        tab[h]["gf"] += hs; tab[h]["gc"] += as_
        tab[a]["gf"] += as_; tab[a]["gc"] += hs
        if hs > as_:
            tab[h]["pts"] += 3
        elif hs < as_:
            tab[a]["pts"] += 3
        else:
            tab[h]["pts"] += 1
            tab[a]["pts"] += 1
    rows = list(tab.values())
    for r in rows:
        r["dg"] = r["gf"] - r["gc"]
    rows.sort(key=lambda r: (-r["pts"], -r["dg"], -r["gf"], r["team"].lower()))
    return rows


# Carga automática de resultados desde ESPN (sin clave) ----------------------
ES2EN = {
    'Argelia': 'Algeria', 'Argentina': 'Argentina', 'Australia': 'Australia',
    'Austria': 'Austria', 'Bélgica': 'Belgium', 'Bosnia': 'Bosnia-Herzegovina',
    'Brasil': 'Brazil', 'Canadá': 'Canada', 'Cabo Verde': 'Cape Verde',
    'Colombia': 'Colombia', 'R.D. Congo': 'Congo DR', 'Croacia': 'Croatia',
    'Curazao': 'Curaçao', 'Rep. Checa': 'Czechia', 'Ecuador': 'Ecuador',
    'Egipto': 'Egypt', 'Inglaterra': 'England', 'Francia': 'France',
    'Alemania': 'Germany', 'Ghana': 'Ghana', 'Haití': 'Haiti', 'Irán': 'Iran',
    'Irak': 'Iraq', 'Costa de Marfil': 'Ivory Coast', 'Japón': 'Japan',
    'Jordania': 'Jordan', 'México': 'Mexico', 'Marruecos': 'Morocco',
    'Países Bajos': 'Netherlands', 'Nueva Zelanda': 'New Zealand',
    'Noruega': 'Norway', 'Panamá': 'Panama', 'Paraguay': 'Paraguay',
    'Portugal': 'Portugal', 'Qatar': 'Qatar', 'Arabia Saudita': 'Saudi Arabia',
    'Escocia': 'Scotland', 'Senegal': 'Senegal', 'Sudáfrica': 'South Africa',
    'Corea del Sur': 'South Korea', 'España': 'Spain', 'Suecia': 'Sweden',
    'Suiza': 'Switzerland', 'Túnez': 'Tunisia', 'Turquía': 'Türkiye',
    'Estados Unidos': 'United States', 'Uruguay': 'Uruguay',
    'Uzbekistán': 'Uzbekistan',
}


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return "".join(ch for ch in s if ch.isalnum())


def _espn_completed_events(dates):
    """Lista de dicts {nombre_normalizado: goles} para partidos TERMINADOS."""
    out = []
    for d in dates:
        url = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
               "fifa.world/scoreboard?dates=" + d)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            continue
        for ev in data.get("events", []):
            try:
                comp = ev["competitions"][0]
                if not comp["status"]["type"].get("completed"):
                    continue
                out.append({_norm(c["team"]["displayName"]): c.get("score")
                            for c in comp["competitors"]})
            except Exception:
                continue
    return out


def _result_for(home_es, away_es, events):
    nh, na = _norm(ES2EN.get(home_es, home_es)), _norm(ES2EN.get(away_es, away_es))
    for teams in events:
        if nh in teams and na in teams:
            try:
                return int(teams[nh]), int(teams[na])
            except (TypeError, ValueError):
                return None
    return None


def autoload_results():
    """Carga marcadores finales (desde ESPN) de partidos terminados sin resultado.
       Devuelve (cargados, pendientes) como listas de texto."""
    pend = Match.query.filter(Match.finished.isnot(True),
                              Match.kickoff <= now_local()).all()
    if not pend:
        return [], []
    dates = set()
    for m in pend:
        for off in (-1, 0, 1):
            dates.add((m.match_date + timedelta(days=off)).strftime("%Y%m%d"))
    events = _espn_completed_events(sorted(dates))
    loaded, missed = [], []
    for m in pend:
        r = _result_for(m.home_team, m.away_team, events)
        if r:
            m.home_score, m.away_score, m.finished = r[0], r[1], True
            loaded.append(f"{m.home_team} {r[0]}-{r[1]} {m.away_team}")
        else:
            missed.append(f"{m.home_team} vs {m.away_team}")
    db.session.commit()
    return loaded, missed


DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


@app.template_filter("fecha_larga")
def fecha_larga(d):
    if not d:
        return ""
    return f"{DIAS[d.weekday()].capitalize()} {d.day} de {MESES[d.month - 1]}"


@app.before_request
def load_player():
    g.player = None
    pid = session.get("player_id")
    if pid:
        g.player = db.session.get(Player, pid)


@app.context_processor
def inject_globals():
    return dict(current_player=getattr(g, "player", None),
                is_admin=session.get("is_admin", False),
                hoy=now_local().date())


# ----------------------------------------------------------------------------
# Rutas: jugador
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    if not g.player:
        return redirect(url_for("login"))
    return redirect(url_for("jugar"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:40]
        secret = request.form.get("secret", "").strip()
        if not name or not secret:
            flash("Escribe tu nombre y tu número secreto 🙂", "error")
            return redirect(url_for("login"))
        p = Player.query.filter(db.func.lower(Player.name) == name.lower()).first()
        if p:
            if not (p.pin_hash and check_password_hash(p.pin_hash, secret)):
                flash("Número secreto incorrecto 🔒", "error")
                return redirect(url_for("login"))
        else:
            if not registration_open():
                flash("La inscripción está cerrada 🔒. Pídele al organizador que te sume.", "error")
                return redirect(url_for("login"))
            if Player.query.count() >= MAX_PLAYERS:
                flash("El juego ya tiene 15 jugadores 😅", "error")
                return redirect(url_for("login"))
            p = Player(name=name, pin_hash=generate_password_hash(secret))
            db.session.add(p)
            db.session.commit()
        session["player_id"] = p.id
        return redirect(url_for("jugar"))
    return render_template("login.html", registration_open=registration_open())


@app.route("/logout")
def logout():
    session.pop("player_id", None)
    return redirect(url_for("login"))


@app.route("/jugar")
def jugar():
    if not g.player:
        return redirect(url_for("login"))
    fs = fechas_disponibles()
    sel = request.args.get("fecha")
    fecha = None
    if sel:
        try:
            fecha = date.fromisoformat(sel)
        except ValueError:
            fecha = None
    if not fecha:
        fecha = fecha_default()

    matches = []
    if fecha:
        matches = (Match.query.filter_by(match_date=fecha)
                   .order_by(Match.kickoff).all())
    my_bets = {}
    if matches:
        ids = [m.id for m in matches]
        for b in Bet.query.filter(Bet.match_id.in_(ids),
                                  Bet.player_id == g.player.id):
            my_bets[b.match_id] = b

    info = []
    for m in matches:
        b = my_bets.get(m.id)
        hp, ap = (b.home_pred, b.away_pred) if b else (0, 0)
        pts = bet_points(hp, ap, m.home_score, m.away_score) if m.finished else None
        info.append(dict(m=m, bet=b, locked=is_locked(m), points=pts))

    return render_template("jugar.html", fecha=fecha, fechas=fs, info=info)


@app.route("/apostar", methods=["POST"])
def apostar():
    if not g.player:
        return redirect(url_for("login"))
    fecha = request.form.get("fecha")
    guardadas = bloqueadas = 0
    for key in list(request.form.keys()):
        if not key.startswith("home_"):
            continue
        try:
            mid = int(key[5:])
        except ValueError:
            continue
        m = db.session.get(Match, mid)
        if not m:
            continue
        if is_locked(m):
            bloqueadas += 1
            continue
        hp = request.form.get(f"home_{mid}", "").strip()
        ap = request.form.get(f"away_{mid}", "").strip()
        try:
            hp = int(hp) if hp != "" else 0
            ap = int(ap) if ap != "" else 0
        except ValueError:
            continue
        if not (0 <= hp <= 99 and 0 <= ap <= 99):
            continue
        b = Bet.query.filter_by(player_id=g.player.id, match_id=mid).first()
        if b:
            b.home_pred, b.away_pred, b.updated_at = hp, ap, now_local()
        else:
            db.session.add(Bet(player_id=g.player.id, match_id=mid,
                               home_pred=hp, away_pred=ap))
        guardadas += 1
    db.session.commit()
    msg = f"¡Listo! Guardé {guardadas} marcador(es). 🎉"
    if bloqueadas:
        msg += f" ({bloqueadas} ya estaban cerrados 🔒)"
    flash(msg, "ok")
    return redirect(url_for("jugar", fecha=fecha))


@app.route("/ranking")
def ranking():
    if not g.player:
        return redirect(url_for("login"))
    fs = fechas_disponibles()
    sel = request.args.get("fecha")
    fecha = None
    if sel:
        try:
            fecha = date.fromisoformat(sel)
        except ValueError:
            pass
    if not fecha:
        fecha = fecha_default()

    players = {p.id: p for p in Player.query.all()}
    cols = player_colors()
    day_rows, day_complete = [], False
    if fecha:
        scores, awards, matches, finished = day_ranking(fecha)
        day_complete = bool(matches) and all(m.finished for m in matches)
        rows = [dict(name=players[pid].name, score=sc, color=cols.get(pid),
                     award=awards.get(pid, 0), me=(pid == g.player.id))
                for pid, sc in scores.items()]
        rows.sort(key=lambda r: (-r["score"], r["name"].lower()))
        day_rows = rows

    overall = overall_ranking()
    gen = [dict(name=players[pid].name, award=d["award"], bet=d["bet"],
                color=cols.get(pid), me=(pid == g.player.id))
           for pid, d in overall.items()]
    gen.sort(key=lambda r: (-r["award"], -r["bet"], r["name"].lower()))

    return render_template("ranking.html", fecha=fecha, fechas=fs,
                           day_rows=day_rows, gen=gen, day_complete=day_complete)


@app.route("/apuestas")
def apuestas():
    if not g.player:
        return redirect(url_for("login"))
    fs = fechas_disponibles()
    sel = request.args.get("fecha")
    fecha = None
    if sel:
        try:
            fecha = date.fromisoformat(sel)
        except ValueError:
            pass
    if not fecha:
        fecha = fecha_default()

    matches = []
    if fecha:
        matches = (Match.query.filter_by(match_date=fecha)
                   .order_by(Match.kickoff).all())
    ids = [m.id for m in matches]
    bet_map = {}
    if ids:
        for b in Bet.query.filter(Bet.match_id.in_(ids)).all():
            bet_map[(b.player_id, b.match_id)] = b

    cols = [dict(m=m, locked=is_locked(m)) for m in matches]
    pcol = player_colors()
    grid = []
    for p in Player.query.order_by(Player.name).all():
        cells = []
        for m in matches:
            if is_locked(m):
                b = bet_map.get((p.id, m.id))
                hp, ap = (b.home_pred, b.away_pred) if b else (0, 0)
                pts = (bet_points(hp, ap, m.home_score, m.away_score)
                       if m.finished else None)
                cells.append(dict(shown=True, pred=f"{hp}-{ap}",
                                  pts=pts, default=(b is None)))
            else:
                cells.append(dict(shown=False))
        grid.append(dict(name=p.name, color=pcol.get(p.id),
                         me=(p.id == g.player.id), cells=cells))
    return render_template("apuestas.html", fecha=fecha, fechas=fs,
                           cols=cols, grid=grid)


@app.route("/grupos")
def grupos():
    if not g.player:
        return redirect(url_for("login"))
    matches = Match.query.all()
    by_group = {}
    for m in matches:
        gl = TEAM_GROUP.get(m.home_team)
        if gl:
            by_group.setdefault(gl, []).append(m)
    teams_by_group = {}
    for t, gl in TEAM_GROUP.items():
        teams_by_group.setdefault(gl, []).append(t)

    my = {b.match_id: b for b in Bet.query.filter_by(player_id=g.player.id)}

    def pred_score(m):
        b = my.get(m.id)
        return (b.home_pred, b.away_pred) if b else (0, 0)

    def real_score(m):
        return (m.home_score, m.away_score) if m.finished else None

    groups = []
    for gl in sorted(by_group):
        ms = by_group[gl]
        teams = teams_by_group.get(gl, [])
        groups.append(dict(
            letter=gl,
            pred=_standings(teams, ms, pred_score),
            real=_standings(teams, ms, real_score),
            jugados=sum(1 for m in ms if m.finished),
            total=len(ms),
        ))
    return render_template("grupos.html", groups=groups)


@app.route("/reglas")
def reglas():
    return render_template("reglas.html")


# --------------------------------------------------------------------------
# API para automatizar la carga de resultados (protegida por PIN)
# --------------------------------------------------------------------------
@app.route("/api/pendientes")
def api_pendientes():
    """Partidos que ya terminaron (kickoff hace +2.5h) y aún sin resultado."""
    if request.args.get("pin") != ADMIN_PIN:
        return jsonify(error="pin"), 403
    cutoff = now_local() - timedelta(minutes=150)
    ms = (Match.query
          .filter(Match.finished.isnot(True), Match.kickoff <= cutoff)
          .order_by(Match.kickoff).all())
    return jsonify([dict(id=m.id, home=m.home_team, away=m.away_team,
                         kickoff=m.kickoff.strftime("%Y-%m-%dT%H:%M")) for m in ms])


@app.route("/api/resultado", methods=["POST"])
def api_resultado():
    """Carga el marcador final de un partido. Form: pin, match_id, home_score, away_score."""
    if request.form.get("pin") != ADMIN_PIN:
        return jsonify(error="pin"), 403
    mid = request.form.get("match_id", "")
    m = db.session.get(Match, int(mid)) if mid.isdigit() else None
    if not m:
        return jsonify(error="match"), 404
    try:
        m.home_score = int(request.form["home_score"])
        m.away_score = int(request.form["away_score"])
        m.finished = True
    except (KeyError, ValueError):
        return jsonify(error="score"), 400
    db.session.commit()
    return jsonify(ok=True, id=m.id, home=m.home_team, away=m.away_team,
                   score=f"{m.home_score}-{m.away_score}")


@app.route("/api/actualizar", methods=["POST"])
def api_actualizar():
    """Busca en ESPN y carga los resultados finales pendientes. Form: pin."""
    if request.form.get("pin") != ADMIN_PIN:
        return jsonify(error="pin"), 403
    loaded, missed = autoload_results()
    return jsonify(ok=True, cargados=loaded, pendientes=missed)


# ----------------------------------------------------------------------------
# Rutas: organizador (admin)
# ----------------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("pin") == ADMIN_PIN:
            session["is_admin"] = True
            flash("Modo organizador activado 🛠️", "ok")
            return redirect(url_for("admin"))
        flash("PIN incorrecto", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("jugar"))


@app.route("/admin/inscripcion", methods=["POST"])
def admin_inscripcion():
    require_admin()
    set_setting("registration_open", "0" if registration_open() else "1")
    flash("Inscripción " + ("abierta ✅" if registration_open() else "cerrada 🔒"), "ok")
    return redirect(url_for("admin"))


@app.route("/admin/jugador/<int:pid>/borrar", methods=["POST"])
def admin_borrar_jugador(pid):
    require_admin()
    p = db.session.get(Player, pid)
    if p:
        name = p.name
        Bet.query.filter_by(player_id=pid).delete()
        db.session.delete(p)
        db.session.commit()
        flash(f"Jugador {name} eliminado 🗑️", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/actualizar", methods=["POST"])
def admin_actualizar():
    require_admin()
    loaded, missed = autoload_results()
    if not loaded and not missed:
        flash("No hay partidos terminados pendientes de resultado. ✅", "ok")
    else:
        msg = f"🔄 Cargué {len(loaded)} resultado(s)."
        if loaded:
            msg += " " + "; ".join(loaded) + "."
        if missed:
            msg += f" ⏳ Aún sin marcador final: {', '.join(missed)}."
        flash(msg, "ok")
    return redirect(url_for("admin"))


def require_admin():
    if not session.get("is_admin"):
        abort(403)


@app.route("/admin")
def admin():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))
    matches = Match.query.order_by(Match.kickoff).all()
    players = Player.query.order_by(Player.name).all()
    return render_template("admin.html", matches=matches, players=players,
                           now=now_local(), registration_open=registration_open(),
                           colors=player_colors())


@app.route("/admin/partido", methods=["POST"])
def admin_crear():
    require_admin()
    home = request.form.get("home_team", "").strip()
    away = request.form.get("away_team", "").strip()
    ko = request.form.get("kickoff", "").strip()
    if not (home and away and ko):
        flash("Faltan datos del partido", "error")
        return redirect(url_for("admin"))
    try:
        kdt = datetime.fromisoformat(ko)          # "YYYY-MM-DDTHH:MM"
    except ValueError:
        flash("Fecha/hora inválida", "error")
        return redirect(url_for("admin"))
    db.session.add(Match(home_team=home, away_team=away,
                         kickoff=kdt, match_date=kdt.date()))
    db.session.commit()
    flash("Partido agregado ⚽", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/partido/<int:mid>/resultado", methods=["POST"])
def admin_resultado(mid):
    require_admin()
    m = db.session.get(Match, mid)
    if not m:
        abort(404)
    hs = request.form.get("home_score", "").strip()
    as_ = request.form.get("away_score", "").strip()
    if hs == "" or as_ == "":
        m.home_score = m.away_score = None
        m.finished = False
    else:
        try:
            m.home_score, m.away_score = int(hs), int(as_)
            m.finished = True
        except ValueError:
            flash("Marcador inválido", "error")
            return redirect(url_for("admin"))
    db.session.commit()
    flash("Resultado guardado ✅", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/partido/<int:mid>/editar", methods=["POST"])
def admin_editar(mid):
    require_admin()
    m = db.session.get(Match, mid)
    if not m:
        abort(404)
    home = request.form.get("home_team", "").strip()
    away = request.form.get("away_team", "").strip()
    ko = request.form.get("kickoff", "").strip()
    if home:
        m.home_team = home
    if away:
        m.away_team = away
    if ko:
        try:
            kdt = datetime.fromisoformat(ko)
            m.kickoff = kdt
            m.match_date = kdt.date()
        except ValueError:
            flash("Fecha/hora inválida", "error")
            return redirect(url_for("admin"))
    db.session.commit()
    flash("Partido actualizado ✏️", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/partido/<int:mid>/borrar", methods=["POST"])
def admin_borrar(mid):
    require_admin()
    m = db.session.get(Match, mid)
    if m:
        Bet.query.filter_by(match_id=mid).delete()
        db.session.delete(m)
        db.session.commit()
        flash("Partido borrado 🗑️", "ok")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
