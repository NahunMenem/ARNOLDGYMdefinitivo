import textwrap
import pytz
import urllib3
import requests
import time
from flask import Flask, json, render_template, request, jsonify,flash
from requests.auth import HTTPDigestAuth
from flask import Flask, render_template, request, jsonify, redirect, url_for
import psycopg2


app = Flask(__name__)
app.secret_key = "supersecreto123"  # ⚡ poné una clave más segura en producción
from flask_sqlalchemy import SQLAlchemy

import os
DATABASE_URL = os.environ.get("DATABASE_URL") or "postgresql://negocio2_user:0reioO9H1lLJqE2IazaFKoZ55ZItnU5X@dpg-d04do9qdbo4c73egutjg-a.oregon-postgres.render.com/negocio2"
INGRESO_DEDUP_SECONDS = 30
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)



# Configuración del lector — cargada desde lector_config.json o variables de entorno
_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lector_config.json')

def _leer_config_lector():
    """Lee la config del lector desde archivo JSON, con fallback a env vars y defaults."""
    import json as _j
    cfg = {
        "ip":   os.environ.get("HIKVISION_IP",   "192.168.1.31"),
        "user": os.environ.get("HIKVISION_USER",  "admin"),
        "pass": os.environ.get("HIKVISION_PASS",  ""),
    }
    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg.update(_j.load(f))
        except Exception:
            pass
    return cfg

_cfg        = _leer_config_lector()
HIKVISION_IP = _cfg["ip"]
USERNAME     = _cfg["user"]
PASSWORD     = _cfg["pass"]
BASE_URL     = f"http://{HIKVISION_IP}/ISAPI"

# Desactivar advertencias por certificado autofirmado
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def _init_db_extras():
    """Crea tablas auxiliares si no existen (se llama al arrancar)."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pases_diarios (
                id          SERIAL PRIMARY KEY,
                nombre      VARCHAR(100) NOT NULL,
                monto       NUMERIC(10,2) NOT NULL,
                metodo_pago VARCHAR(30),
                fecha       TIMESTAMPTZ DEFAULT NOW(),
                notas       TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auditoria (
                id         SERIAL PRIMARY KEY,
                fecha      TIMESTAMPTZ DEFAULT NOW(),
                accion     VARCHAR(60)  NOT NULL,
                detalle    TEXT,
                ip_cliente VARCHAR(45)
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[INIT DB] {e}")

_init_db_extras()


def _log_auditoria(accion, detalle, ip=''):
    """Registra una acción en la tabla auditoria. No lanza excepciones."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO auditoria (accion, detalle, ip_cliente) VALUES (%s, %s, %s)",
            (accion, detalle or '', ip or '')
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[AUDIT ERROR] {e}")

# ── Sesión HTTP persistente ────────────────────────────────────────────────────
# Reutiliza la conexión TCP en lugar de abrir/cerrar una por cada llamada.
def _crear_sesion_hikvision():
    s = requests.Session()
    s.auth   = HTTPDigestAuth(USERNAME, PASSWORD)
    s.verify = False
    return s

hikvision_session = _crear_sesion_hikvision()

# ── Helper con reintentos automáticos ─────────────────────────────────────────
_ERRORES_REINTENTABLES = (
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectionError,
)

def _hik_request(method, url, max_reintentos=3, pausa=1.5, **kwargs):
    """
    Llama al lector Hikvision con reintentos automáticos.
    Si falla por timeout o conexión, espera `pausa` segundos y vuelve a intentar.
    Lanza la última excepción si se agotan los intentos.
    """
    kwargs.setdefault('timeout', 8)   # más generoso que el timeout anterior (5 s)
    ultimo_error = None
    for intento in range(max_reintentos):
        try:
            return hikvision_session.request(method, url, **kwargs)
        except _ERRORES_REINTENTABLES as e:
            ultimo_error = e
            if intento < max_reintentos - 1:
                print(f"[LECTOR] Intento {intento + 1}/{max_reintentos} fallido "
                      f"({type(e).__name__}). Reintentando en {pausa} s…")
                time.sleep(pausa)
    raise ultimo_error

# Modelo de base de datos para usuarios--------------------------------------------------------------------------------------------------------
class UsuarioLector(db.Model):
    __tablename__ = 'usuarios_lector'
    id = db.Column(db.Integer, primary_key=True)
    legajo = db.Column(db.String(20), unique=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=False)
    genero = db.Column(db.String(20))
    fecha_nacimiento = db.Column(db.Date)
    telefono = db.Column(db.String(30))
    valido_hasta = db.Column(db.Date)


@app.route('/cargar_usuario', methods=['POST'])
def cargar_usuario():
    data = request.json
    nombre = data.get("nombre")
    legajo = data.get("legajo")
    genero = data.get("genero")
    fecha_nacimiento = data.get("fecha_nacimiento")  # yyyy-mm-dd
    telefono = data.get("telefono")
    valido_hasta = data.get("valido_hasta") + " 00:00:00"

    payload = {
        "UserInfo": {
            "employeeNo": legajo,
            "name": nombre,
            "userType": "normal",
            "Valid": {
                "enable": True,
                "beginTime": "2024-01-01T00:00:00",
                "endTime": valido_hasta.replace(" ", "T")
            },
            "doorRight": "1"
        }
    }

    try:
        # Cargar usuario en el lector
        res = _hik_request(
            'POST',
            f"{BASE_URL}/AccessControl/UserInfo/Record?format=json",
            json=payload,
            headers={"Content-Type": "application/json"},
        )

        # Cargar o actualizar en la base de datos
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO usuarios_lector (nombre, legajo, genero, fecha_nacimiento, telefono, valido_hasta)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (legajo) DO UPDATE SET
              nombre = EXCLUDED.nombre,
              genero = EXCLUDED.genero,
              fecha_nacimiento = EXCLUDED.fecha_nacimiento,
              telefono = EXCLUDED.telefono,
              valido_hasta = EXCLUDED.valido_hasta
        """, (
            nombre,
            legajo,
            genero,
            fecha_nacimiento if fecha_nacimiento else None,
            telefono,
            valido_hasta
        ))
        conn.commit()
        cur.close()
        conn.close()

        _log_auditoria(
            'NUEVO_SOCIO',
            f"Legajo {legajo} — {nombre}",
            ip=request.remote_addr
        )
        return jsonify({"status": res.status_code, "response": res.text})

    except Exception as e:
        return jsonify({"error": str(e)}), 500




@app.route('/formulario_usuario')
def formulario_usuario():
    return render_template('cargar_usuario.html')




# Ruta para listar usuarios-------------------------------------------------------------------------------------------
from datetime import date, timedelta, timezone

from flask import request
from datetime import date

@app.route('/listar_usuarios')
def listar_usuarios():
    ip_cliente = request.remote_addr
    print(f"IP del cliente: {ip_cliente}")

    page         = request.args.get('page', 1, type=int)
    busqueda     = request.args.get('busqueda', '').strip().lower()
    filtro_genero     = request.args.get('genero', '')
    filtro_membresia  = request.args.get('membresia', '')
    per_page = 25

    if ip_cliente.startswith("192.168.") or ip_cliente == "127.0.0.1":
        return listar_usuarios_lector(page, per_page, busqueda, filtro_genero, filtro_membresia)
    else:
        return listar_usuarios_bd(page, per_page, busqueda, filtro_genero, filtro_membresia)


def listar_usuarios_lector(page, per_page, busqueda, filtro_genero, filtro_membresia):
    url = f"{BASE_URL}/AccessControl/UserInfo/Search?format=json"
    payload = {
        "UserInfoSearchCond": {
            "searchID": "1",
            "maxResults": 500,
            "searchResultPosition": 0
        }
    }

    try:
        res = _hik_request(
            'POST', url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        usuarios_lector = res.json().get("UserInfoSearch", {}).get("UserInfo", [])

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT legajo, nombre, genero, fecha_nacimiento, telefono, valido_hasta FROM usuarios_lector")
        datos_db = cur.fetchall()
        conn.close()

        datos_dict = {
            str(f[0]): {
                "nombre": f[1],
                "genero": f[2],
                "fecha_nacimiento": f[3].isoformat() if f[3] else None,
                "telefono": f[4],
                "valido_hasta": f[5].isoformat() if f[5] else None
            }
            for f in datos_db
        }

        fecha_hoy = date.today().isoformat()

        fusionados = {}
        for u in usuarios_lector:
            legajo = str(u.get("employeeNo"))
            fusionados[legajo] = {
                "employeeNo": legajo,
                "name": u.get("name"),
                "genero": None,
                "fecha_nacimiento": None,
                "telefono": None,
                "Valid": u.get("Valid"),
                "valido_hasta": None
            }

        for legajo, datos in datos_dict.items():
            if legajo in fusionados:
                fusionados[legajo].update(datos)
            else:
                fusionados[legajo] = {
                    "employeeNo": legajo,
                    "name": datos["nombre"],
                    "genero": datos["genero"],
                    "fecha_nacimiento": datos["fecha_nacimiento"],
                    "telefono": datos["telefono"],
                    "valido_hasta": datos["valido_hasta"],
                    "Valid": None
                }

        for u in fusionados.values():
            fecha_validez = u.get("valido_hasta") or (u.get("Valid", {}).get("endTime")[:10] if u.get("Valid") else None)
            if fecha_validez:
                u["membresia"] = "Vigente" if fecha_validez >= fecha_hoy else "Vencido"
            else:
                u["membresia"] = "Sin datos"

        todos = list(fusionados.values())
        total_lector = len(todos)

        # Aplicar filtros
        if busqueda:
            todos = [u for u in todos if busqueda in (u.get("name") or "").lower()]
        if filtro_genero:
            todos = [u for u in todos if u.get("genero") == filtro_genero]
        if filtro_membresia:
            todos = [u for u in todos if u.get("membresia") == filtro_membresia]

        total = len(todos)
        pages = max(1, (total + per_page - 1) // per_page)
        page  = max(1, min(page, pages))
        start = (page - 1) * per_page
        usuarios_pagina = todos[start:start + per_page]

        return render_template("lista_usuarios.html",
            usuarios=usuarios_pagina,
            total_lector=total_lector,
            total=total,
            page=page,
            pages=pages,
            per_page=per_page,
            busqueda=busqueda,
            filtro_genero=filtro_genero,
            filtro_membresia=filtro_membresia,
        )

    except Exception as e:
        return f"Error al obtener usuarios: {str(e)}"


def listar_usuarios_bd(page, per_page, busqueda, filtro_genero, filtro_membresia):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        where_clauses = []
        params = []

        if busqueda:
            where_clauses.append("LOWER(nombre) LIKE %s")
            params.append(f"%{busqueda}%")
        if filtro_genero:
            where_clauses.append("genero = %s")
            params.append(filtro_genero)
        if filtro_membresia == "Vigente":
            where_clauses.append("valido_hasta >= CURRENT_DATE")
        elif filtro_membresia == "Vencido":
            where_clauses.append("(valido_hasta IS NULL OR valido_hasta < CURRENT_DATE)")

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        cur.execute("SELECT COUNT(*) FROM usuarios_lector")
        total_lector = cur.fetchone()[0]

        cur.execute(f"SELECT COUNT(*) FROM usuarios_lector{where_sql}", params)
        total = cur.fetchone()[0]

        pages = max(1, (total + per_page - 1) // per_page)
        page  = max(1, min(page, pages))
        offset = (page - 1) * per_page

        cur.execute(f"""
            SELECT nombre, legajo, genero, fecha_nacimiento, telefono, valido_hasta
            FROM usuarios_lector{where_sql}
            ORDER BY nombre
            LIMIT %s OFFSET %s
        """, params + [per_page, offset])
        usuarios_db = cur.fetchall()
        conn.close()

        fecha_hoy = date.today()
        usuarios = []
        for u in usuarios_db:
            usuarios.append({
                "name": u[0],
                "employeeNo": u[1],
                "genero": u[2],
                "fecha_nacimiento": u[3].isoformat() if u[3] else None,
                "telefono": u[4],
                "valido_hasta": u[5].isoformat() if u[5] else None,
                "membresia": "Vigente" if u[5] and u[5] >= fecha_hoy else "Vencido"
            })

        return render_template("lista_usuarios.html",
            usuarios=usuarios,
            total_lector=total_lector,
            total=total,
            page=page,
            pages=pages,
            per_page=per_page,
            busqueda=busqueda,
            filtro_genero=filtro_genero,
            filtro_membresia=filtro_membresia,
        )

    except Exception as e:
        return f"Error al obtener usuarios desde la DB: {str(e)}"





@app.route('/eliminar_usuario/<string:employee_no>', methods=['POST'])
def eliminar_usuario(employee_no):
    # 1. Eliminar del lector
    url = f"{BASE_URL}/AccessControl/UserInfo/Delete?format=json"
    payload = {
        "UserInfoDelCond": {
            "EmployeeNoList": [
                {"employeeNo": employee_no}
            ]
        }
    }

    try:
        res = _hik_request(
            'PUT', url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )

        # 2. Si el lector lo borró, borrar pagos y usuario de la DB
        if res.status_code == 200:
            try:
                conn = psycopg2.connect(DATABASE_URL)
                cur = conn.cursor()

                # Primero borrar pagos asociados
                cur.execute("DELETE FROM pagos_lector WHERE legajo = %s", (employee_no,))
                # Luego borrar el usuario
                cur.execute("DELETE FROM usuarios_lector WHERE legajo = %s", (employee_no,))

                conn.commit()
                cur.close()
                conn.close()
                print(f"Usuario {employee_no} y sus pagos eliminados de la base de datos.")
                _log_auditoria(
                    'ELIMINAR_SOCIO',
                    f"Legajo {employee_no} eliminado del sistema y del lector",
                    ip=request.remote_addr
                )
            except Exception as e:
                print(f"[ERROR] No se pudo eliminar de la base: {e}")

        else:
            print(f"[ERROR LECTOR] {res.status_code} - {res.text}")

        return jsonify({"status": res.status_code, "response": res.text})

    except Exception as e:
        return jsonify({"error": str(e)}), 500









# aca vamos a ver los logs de ingreso--------------------------------------------------------------------------------------------







# Ruta para editar un usuario existente-----------------------------------------------------------------------------------------
@app.route('/editar_usuario', methods=['POST'])
def editar_usuario():
    legajo = request.form['legajo_editar']
    nombre = request.form['nombre']
    genero = request.form.get('genero') or None
    fecha_nac = request.form.get('fecha_nacimiento') or None
    telefono = request.form.get('telefono') or None

    # Actualizar en la base de datos local
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            UPDATE usuarios_lector
            SET genero = %s, fecha_nacimiento = %s, telefono = %s
            WHERE legajo = %s
        """, (genero, fecha_nac, telefono, legajo))
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({"error": "Error actualizando en base de datos", "detalle": str(e)}), 500

    # Opcional: actualizar también en el lector (si querés cambiar nombre o algo)
    payload = {
        "UserInfo": {
            "employeeNo": legajo,
            "name": nombre,
            "userType": "normal",
            "Valid": {
                "enable": True,
                "beginTime": "2024-01-01T00:00:00",
                "endTime": "2030-01-01T00:00:00"  # Fecha arbitraria
            },
            "doorRight": "1"
        }
    }

    url = f"{BASE_URL}/AccessControl/UserInfo/Modify?format=json"

    try:
        res = _hik_request(
            'PUT', url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        print("Editar usuario:", res.status_code, res.text)
        return redirect("/listar_usuarios")
    except Exception as e:
        return jsonify({"error": "Error actualizando en el lector", "detalle": str(e)}), 500




# Ruta para el dashboard de estadísticas------------------------------------------------------------------------------------------------------------------------------
from flask import request

@app.route('/dashboard')
def dashboard():
    from datetime import datetime
    fecha_desde = request.args.get('desde')
    fecha_hasta = request.args.get('hasta')

    condiciones = []
    valores = []

    if fecha_desde:
        condiciones.append("(p.fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')::date >= %s")
        valores.append(fecha_desde)
    if fecha_hasta:
        condiciones.append("(p.fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')::date <= %s")
        valores.append(fecha_hasta)

    where_clause = f"WHERE {' AND '.join(condiciones)}" if condiciones else ""

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM usuarios_lector")
    total = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM usuarios_lector WHERE genero = 'Masculino'")
    hombres = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM usuarios_lector WHERE genero = 'Femenino'")
    mujeres = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM usuarios_lector WHERE genero NOT IN ('Masculino', 'Femenino') OR genero IS NULL")
    otros = cur.fetchone()[0]

    # Ingresos membresías
    cur.execute(f"SELECT COALESCE(SUM(p.monto), 0) FROM pagos_lector p {where_clause}", valores)
    total_membresias = cur.fetchone()[0]

    # Ingresos pases diarios
    where_pd = where_clause.replace("p.fecha", "pd.fecha").replace("p.monto", "pd.monto")
    cur.execute(f"SELECT COALESCE(SUM(pd.monto), 0) FROM pases_diarios pd {where_pd}", valores)
    total_pases = cur.fetchone()[0]

    total_ingresos = float(total_membresias) + float(total_pases)

    # Pases diarios de hoy
    cur.execute("""
        SELECT COUNT(*), COALESCE(SUM(monto), 0) FROM pases_diarios
        WHERE (fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
              = (NOW() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
    """)
    pases_hoy_count, pases_hoy_total = cur.fetchone()

    # Ingresos por método (membresías + pases juntos)
    cur.execute(f"""
        SELECT metodo_pago, SUM(monto) FROM pagos_lector p {where_clause} GROUP BY metodo_pago
        UNION ALL
        SELECT metodo_pago, SUM(monto) FROM pases_diarios pd {where_pd} GROUP BY metodo_pago
    """, valores + valores)
    raw_metodos = cur.fetchall()
    # Agrupar por método en Python
    metodo_dict = {}
    for metodo, monto in raw_metodos:
        metodo_dict[metodo or 'Sin especificar'] = metodo_dict.get(metodo or 'Sin especificar', 0) + float(monto)
    ingresos_por_metodo = sorted(metodo_dict.items(), key=lambda x: x[1], reverse=True)

    # Ingresos por cliente (solo membresías)
    cur.execute(f"""
        SELECT u.nombre, SUM(p.monto)
        FROM pagos_lector p
        JOIN usuarios_lector u ON u.legajo = p.legajo
        {where_clause}
        GROUP BY u.nombre
        ORDER BY SUM(p.monto) DESC
    """, valores)
    ingresos_por_cliente = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        'dashboard.html',
        total=total,
        hombres=hombres,
        mujeres=mujeres,
        otros=otros,
        total_ingresos=total_ingresos,
        total_membresias=float(total_membresias),
        total_pases=float(total_pases),
        pases_hoy_count=pases_hoy_count,
        pases_hoy_total=float(pases_hoy_total),
        ingresos_por_metodo=ingresos_por_metodo,
        ingresos_por_cliente=ingresos_por_cliente,
        desde=fecha_desde,
        hasta=fecha_hasta
    )




# Ruta para registrar un pago de usuario-----------------------------------------------------------------------------------------
@app.route('/registrar_pago', methods=['POST'])
def registrar_pago():
    from datetime import datetime
    from pytz import timezone

    legajo = request.form.get('legajo_pago')
    nuevo_valido_hasta = request.form.get('nuevo_valido_hasta')
    monto = request.form.get('monto_pago')
    metodo_pago = request.form.get('metodo_pago')

    # ✅ Validar y formatear fecha
    try:
        fecha_valida = datetime.strptime(nuevo_valido_hasta, '%Y-%m-%d').date()
        fecha_iso = fecha_valida.strftime('%Y-%m-%dT23:59:59')
    except ValueError:
        return "Fecha inválida", 400

    # ✅ Obtener hora actual de Argentina
    ahora_arg = datetime.now(timezone('America/Argentina/Buenos_Aires'))

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # 🧩 Verificar si ya existe un pago igual el mismo día
        cur.execute("""
            SELECT COUNT(*) FROM pagos_lector
            WHERE legajo = %s
              AND metodo_pago = %s
              AND monto = %s
              AND fecha::date = CURRENT_DATE
        """, (legajo, metodo_pago, monto))
        existe = cur.fetchone()[0]

        if existe > 0:
            # 🔒 Evitamos duplicado y devolvemos aviso
            cur.close()
            conn.close()
            print(f"[⚠️ Duplicado] Pago ya existente para legajo {legajo} hoy.")
            flash("Pago duplicado detectado. No se registró nuevamente.", "warning")
            return redirect('/listar_usuarios')

        # ✅ Actualizar fecha de membresía del usuario
        cur.execute("""
            UPDATE usuarios_lector
            SET valido_hasta = %s
            WHERE legajo = %s
        """, (fecha_valida, legajo))

        # ✅ Insertar el pago con hora local
        cur.execute("""
            INSERT INTO pagos_lector (legajo, monto, fecha, metodo_pago)
            VALUES (%s, %s, %s, %s)
        """, (legajo, monto, ahora_arg, metodo_pago))

        conn.commit()
        cur.close()
        conn.close()

    except Exception as e:
        print("[ERROR BD]", e)
        return f"Error al guardar en base de datos: {e}", 500

    # ✅ Enviar al lector Hikvision
    payload = {
        "UserInfo": {
            "employeeNo": legajo,
            "Valid": {
                "enable": True,
                "beginTime": datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
                "endTime": fecha_iso
            }
        }
    }

    try:
        res = _hik_request(
            'PUT',
            f"{BASE_URL}/AccessControl/UserInfo/Modify?format=json",
            json=payload,
            headers={"Content-Type": "application/json"},
        )

        if res.status_code != 200:
            print(f"[ERROR LECTOR] {res.status_code} - {res.text}")
            flash("Error al actualizar el lector, pero el pago fue registrado.", "warning")

    except Exception as e:
        print("[ERROR CONEXIÓN LECTOR]", e)
        flash("No se pudo conectar con el lector, pero el pago fue registrado.", "warning")

    _log_auditoria(
        'NUEVA_MEMBRESIA',
        f"Legajo {legajo} — ${monto} ({metodo_pago}) — válido hasta {nuevo_valido_hasta}",
        ip=request.remote_addr
    )
    flash("✅ Pago registrado correctamente.", "success")
    return redirect('/listar_usuarios')





# Ruta para ver transacciones de pagos-----------------------------------------------------------------------------------------
from flask import request, render_template
from datetime import datetime
import psycopg2

@app.route('/transacciones', methods=['GET'])
def ver_transacciones():
    fecha_desde = request.args.get('desde')
    fecha_hasta = request.args.get('hasta')

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur  = conn.cursor()
        zona_arg = pytz.timezone('America/Argentina/Buenos_Aires')

        # ── Filtros para cada tabla ──────────────────────────
        cond_p, params_p = [], []
        cond_pd, params_pd = [], []
        if fecha_desde:
            cond_p.append("(p.fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')::date >= %s")
            params_p.append(fecha_desde)
            cond_pd.append("(pd.fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')::date >= %s")
            params_pd.append(fecha_desde)
        if fecha_hasta:
            cond_p.append("(p.fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')::date <= %s")
            params_p.append(fecha_hasta)
            cond_pd.append("(pd.fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')::date <= %s")
            params_pd.append(fecha_hasta)
        where_p  = ("WHERE " + " AND ".join(cond_p))  if cond_p  else ""
        where_pd = ("WHERE " + " AND ".join(cond_pd)) if cond_pd else ""

        # ── UNION membresías + pases del día ─────────────────
        query = f"""
            SELECT p.id,  p.fecha,  p.monto, p.metodo_pago,
                   u.legajo, u.nombre, 'membresia' AS tipo, NULL AS notas
            FROM   pagos_lector p
            JOIN   usuarios_lector u ON p.legajo = u.legajo
            {where_p}

            UNION ALL

            SELECT pd.id, pd.fecha, pd.monto, pd.metodo_pago,
                   NULL, pd.nombre, 'pase_dia' AS tipo, pd.notas
            FROM   pases_diarios pd
            {where_pd}

            ORDER BY fecha DESC
        """
        cur.execute(query, params_p + params_pd)
        rows = cur.fetchall()

        # ── Totales ──────────────────────────────────────────
        cur.execute(f"""
            SELECT COALESCE(SUM(p.monto),0) FROM pagos_lector p {where_p}
        """, params_p)
        total_membresias = float(cur.fetchone()[0])

        cur.execute(f"""
            SELECT COALESCE(SUM(pd.monto),0) FROM pases_diarios pd {where_pd}
        """, params_pd)
        total_pases = float(cur.fetchone()[0])

        cur.close()
        conn.close()

        pagos = [
            {
                "id":          row[0],
                "fecha":       row[1].astimezone(zona_arg) if row[1] else None,
                "monto":       float(row[2]),
                "metodo_pago": row[3],
                "legajo":      row[4],
                "nombre":      row[5],
                "tipo":        row[6],   # 'membresia' | 'pase_dia'
                "notas":       row[7],
            }
            for row in rows
        ]

        return render_template(
            "transacciones.html",
            pagos=pagos,
            total_membresias=total_membresias,
            total_pases=total_pases,
            total_general=total_membresias + total_pases,
            desde=fecha_desde,
            hasta=fecha_hasta
        )

    except Exception as e:
        return f"Error al cargar transacciones: {e}", 500


@app.route('/transacciones/anular', methods=['POST'])
def anular_transaccion():
    """Elimina una transacción de membresía O un pase del día según 'tipo'."""
    tid  = request.form.get('id',   type=int)
    tipo = request.form.get('tipo', 'membresia')
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur  = conn.cursor()
        # Guardar datos antes de borrar para el log
        detalle_log = f"ID {tid} — tipo: {tipo}"
        try:
            if tipo == 'pase_dia':
                cur.execute("SELECT nombre, monto, metodo_pago FROM pases_diarios WHERE id = %s", (tid,))
            else:
                cur.execute("""
                    SELECT u.nombre, p.monto, p.metodo_pago
                    FROM pagos_lector p JOIN usuarios_lector u ON p.legajo = u.legajo
                    WHERE p.id = %s
                """, (tid,))
            row = cur.fetchone()
            if row:
                detalle_log = f"ID {tid} — {row[0]} — ${row[1]} ({row[2]}) — tipo: {tipo}"
        except Exception:
            pass
        if tipo == 'pase_dia':
            cur.execute("DELETE FROM pases_diarios WHERE id = %s", (tid,))
        else:
            cur.execute("DELETE FROM pagos_lector  WHERE id = %s", (tid,))
        conn.commit()
        cur.close()
        conn.close()
        _log_auditoria('ELIMINAR_TRANSACCION', detalle_log, ip=request.remote_addr)
        flash("Transacción eliminada correctamente", "success")
    except Exception as e:
        flash(f"Error al eliminar: {e}", "error")
    return redirect(url_for('ver_transacciones'))




from pytz import timezone
# Ruta para ver registros de ingreso-----------------------------------------------------------------------------------------
@app.route('/registros_ingreso')
def registros_ingreso():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT legajo, nombre, MAX(fecha) AS fecha
            FROM ingresos_lector
            WHERE (fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
                  = (NOW() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
            GROUP BY legajo, nombre, date_trunc('minute', fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')
            ORDER BY fecha DESC
        """)
        logs = cur.fetchall()
        cur.close()
        conn.close()

        # Convertir a zona horaria de Argentina
        zona_arg = timezone('America/Argentina/Buenos_Aires')
        logs_arg = [(l, n, f.astimezone(zona_arg)) for l, n, f in logs]

        return render_template("registros_ingreso.html", logs=logs_arg)

    except Exception as e:
        return f"Error al obtener ingresos: {e}", 500


@app.route('/registros_ingreso_parcial')
def registros_ingreso_parcial():
    """Retorna solo las filas <tr> de ingresos de hoy — usado por HTMX para auto-refresh."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT legajo, nombre, MAX(fecha) AS fecha
            FROM ingresos_lector
            WHERE (fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
                  = (NOW() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
            GROUP BY legajo, nombre, date_trunc('minute', fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')
            ORDER BY fecha DESC
        """)
        logs = cur.fetchall()
        cur.close(); conn.close()

        zona_arg = pytz.timezone('America/Argentina/Buenos_Aires')
        logs_arg = [(l, n, f.astimezone(zona_arg)) for l, n, f in logs]
        return render_template("_registros_rows.html", logs=logs_arg)
    except Exception as e:
        return f"<tr><td colspan='3' class='p-4 text-red-400'>Error: {e}</td></tr>", 500




from flask import request, jsonify
from xml.etree import ElementTree as ET
from datetime import datetime
import psycopg2
from flask import Flask, request, jsonify

import traceback  # asegurate de tenerlo arriba
from pytz import timezone
from datetime import datetime
# Logs---------------------------------------------------------------------------------------------------
from flask import request, jsonify
import json, xml.etree.ElementTree as ET
from datetime import datetime
from pytz import timezone
import psycopg2

from flask import request
import json, xml.etree.ElementTree as ET
from datetime import datetime
from pytz import timezone
import psycopg2

@app.route('/notificar_evento', methods=['GET','POST'])
def notificar_evento():
    print("== NUEVO EVENTO ==")
    print("IP origen:", request.remote_addr)
    # Leer body solo si no es multipart (evita imprimir bytes binarios de imágenes adjuntas)
    content_type = request.headers.get('Content-Type', '')
    if 'multipart' in content_type:
        body_text = ""
    else:
        body_text = request.get_data(as_text=True) or ""
        print("Body crudo:\n", body_text[:2000])

    legajo = nombre = verify_mode = None

    if 'event_log' in request.form:
        try:
            data = json.loads(request.form['event_log'])
            evt = data.get('AccessControllerEvent', {})
            verify_mode = evt.get('currentVerifyMode')
            legajo = evt.get('employeeNoString')
            nombre = evt.get('name')
        except Exception as e:
            print("JSON en form parse error:", e)
    elif request.headers.get('Content-Type','').startswith('application/json'):
        data = request.get_json(silent=True) or {}
        evt = data.get('AccessControllerEvent', {})
        verify_mode = evt.get('currentVerifyMode')
        legajo = evt.get('employeeNoString')
        nombre  = evt.get('name')
    elif body_text.strip().startswith('<'):
        try:
            root = ET.fromstring(body_text)
            def t(tag):
                n = root.find(f'.//{tag}')
                return n.text.strip() if n is not None and n.text else None
            verify_mode = t('currentVerifyMode') or t('verifyMode')
            legajo = t('employeeNoString') or t('employeeNo')
            nombre = t('name') or t('personName')
        except Exception as e:
            print("XML parse error:", e)

    if legajo and nombre:
        try:
            ahora_arg = datetime.now(timezone('America/Argentina/Buenos_Aires'))
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("""
                SELECT id, fecha
                FROM ingresos_lector
                WHERE legajo = %s
                  AND fecha >= %s - (%s * interval '1 second')
                ORDER BY fecha DESC
                LIMIT 1
            """, (legajo, ahora_arg, INGRESO_DEDUP_SECONDS))
            duplicado = cur.fetchone()

            if duplicado:
                cur.close(); conn.close()
                print(f"[BD] Ingreso duplicado ignorado: {nombre} ({legajo})")
                return "OK", 200

            cur.execute("INSERT INTO ingresos_lector (legajo, nombre, fecha) VALUES (%s,%s,%s)",
                        (legajo, nombre, ahora_arg))
            conn.commit()
            cur.close(); conn.close()
            print(f"[BD] Ingreso registrado: {nombre} ({legajo})")
        except Exception as e:
            print("[ERROR BD]", e)
    else:
        print("[WARNING] No pude extraer legajo/nombre. verify_mode:", verify_mode)

    return "OK", 200




@app.route('/pantalla_acceso')
def pantalla_acceso():
    return render_template("pantalla_acceso.html")

@app.route('/')
def inicio():
    return redirect(url_for('dashboard'))


from flask import jsonify
from datetime import datetime
from pytz import timezone

from flask import jsonify
from pytz import timezone
from datetime import datetime

@app.route('/api/ultimo_ingreso')
def api_ultimo_ingreso():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute("""
            SELECT legajo, nombre, fecha
            FROM ingresos_lector
            ORDER BY fecha DESC
            LIMIT 1
        """)
        ingreso = cur.fetchone()

        if not ingreso:
            return jsonify({"estado": "esperando", "mensaje": "Esperando ingreso..."})

        legajo, nombre, fecha = ingreso

        # Traer fecha de vencimiento desde usuarios_lector
        cur.execute("SELECT valido_hasta FROM usuarios_lector WHERE legajo = %s", (legajo,))
        resultado = cur.fetchone()

        cur.close()
        conn.close()

        if not resultado:
            return jsonify({
                "estado": "incorrecto",
                "nombre": nombre,
                "mensaje": "Cliente no encontrado en sistema.",
                "fecha_vencimiento": None,
                "dias_restantes": 0,
                "legajo": legajo
            })

        valido_hasta = resultado[0]
        ahora = datetime.now(timezone('America/Argentina/Buenos_Aires')).date()

        dias_restantes = (valido_hasta - ahora).days
        fecha_str = valido_hasta.strftime('%Y-%m-%d')  # Para que JS lo entienda

        if valido_hasta >= ahora:
            return jsonify({
                "estado": "correcto",
                "nombre": nombre,
                "fecha_vencimiento": fecha_str,
                "dias_restantes": dias_restantes,
                "legajo": legajo
            })
        else:
            return jsonify({
                "estado": "incorrecto",
                "nombre": nombre,
                "fecha_vencimiento": fecha_str,
                "dias_restantes": dias_restantes,
                "legajo": legajo
            })

    except Exception as e:
        print("[API ERROR]", e)
        return jsonify({"estado": "error", "mensaje": str(e)}), 500



from flask import jsonify
from datetime import datetime
import psycopg2

@app.route('/api/cumpleanios_hoy')
def api_cumpleanios_hoy():
    """Devuelve los socios que cumplen años HOY (día y mes). Incluye teléfono."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur  = conn.cursor()
        hoy  = datetime.now(pytz.timezone('America/Argentina/Buenos_Aires'))
        cur.execute("""
            SELECT nombre, telefono, TO_CHAR(fecha_nacimiento, 'DD/MM') as fecha
            FROM usuarios_lector
            WHERE EXTRACT(DAY   FROM fecha_nacimiento) = %s
              AND EXTRACT(MONTH FROM fecha_nacimiento) = %s
            ORDER BY nombre
        """, (hoy.day, hoy.month))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([
            {"nombre": r[0], "telefono": r[1] or "", "fecha": r[2]}
            for r in rows
        ])
    except Exception as e:
        return jsonify([]), 500


@app.route('/api/cumples_mes')
def api_cumples_mes():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        hoy = datetime.now()
        mes_actual = hoy.month

        cur.execute("""
            SELECT nombre, TO_CHAR(fecha_nacimiento, 'DD/MM') as fecha
            FROM usuarios_lector
            WHERE EXTRACT(MONTH FROM fecha_nacimiento) = %s
            ORDER BY EXTRACT(DAY FROM fecha_nacimiento)
        """, (mes_actual,))
        resultados = cur.fetchall()

        cur.close()
        conn.close()

        lista = [{"nombre": r[0], "fecha": r[1]} for r in resultados]
        return jsonify(lista)

    except Exception as e:
        print("[ERROR CUMPLES]", e)
        return jsonify([]), 500

@app.route('/cumpleanios')
def cumpleanios():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        hoy = datetime.now()
        mes_actual = hoy.month

        cur.execute("""
            SELECT legajo, nombre, telefono, TO_CHAR(fecha_nacimiento, 'DD/MM') as fecha
            FROM usuarios_lector
            WHERE EXTRACT(MONTH FROM fecha_nacimiento) = %s
            ORDER BY EXTRACT(DAY FROM fecha_nacimiento)
        """, (mes_actual,))
        resultados = cur.fetchall()

        usuarios = []
        for row in resultados:
            usuarios.append({
                "employeeNo": row[0],
                "name": row[1],
                "telefono": row[2] or "-",
                "fecha": row[3]
            })

        cur.close()
        conn.close()

        return render_template("cumpleanios.html", usuarios=usuarios)

    except Exception as e:
        return f"Error al obtener cumpleañeros: {e}", 500



@app.route('/api/ultimo_pago/<legajo>')
def api_ultimo_pago(legajo):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute("""
            SELECT u.nombre, p.monto, p.metodo_pago, p.fecha, u.valido_hasta
            FROM pagos_lector p
            JOIN usuarios_lector u ON p.legajo = u.legajo
            WHERE p.legajo = %s
            ORDER BY p.fecha DESC
            LIMIT 1
        """, (legajo,))
        row = cur.fetchone()

        cur.close()
        conn.close()

        if row:
            return jsonify({
                "nombre": row[0],
                "monto": float(row[1]),
                "metodo_pago": row[2],
                "fecha": row[3].strftime('%d-%m-%Y'),
                "valido_hasta": row[4].strftime('%d-%m-%Y') if row[4] else "-"
            })
        else:
            return jsonify({"error": "No se encontró pago"}), 404

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/usuarios_inactivos')
def usuarios_inactivos():
    per_page = 25
    page     = request.args.get('page', 1, type=int)
    busqueda = request.args.get('busqueda', '').strip().lower()

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        where_extra = ""
        params = []
        if busqueda:
            where_extra = " AND LOWER(u.nombre) LIKE %s"
            params.append(f"%{busqueda}%")

        cur.execute(f"""
            SELECT COUNT(*)
            FROM usuarios_lector u
            WHERE u.valido_hasta IS NOT NULL
                AND u.valido_hasta < CURRENT_DATE
                {where_extra}
        """, params)
        total = cur.fetchone()[0]

        pages = max(1, (total + per_page - 1) // per_page)
        page  = max(1, min(page, pages))
        offset = (page - 1) * per_page

        cur.execute(f"""
            SELECT
                u.legajo,
                u.nombre,
                u.genero,
                u.fecha_nacimiento,
                u.telefono,
                u.valido_hasta,
                MAX(i.fecha) AS ultima_fecha
            FROM usuarios_lector u
            LEFT JOIN ingresos_lector i ON u.legajo = i.legajo
            WHERE u.valido_hasta IS NOT NULL
                AND u.valido_hasta < CURRENT_DATE
                {where_extra}
            GROUP BY
                u.legajo, u.nombre, u.genero, u.fecha_nacimiento, u.telefono, u.valido_hasta
            ORDER BY u.nombre ASC
            LIMIT %s OFFSET %s
        """, params + [per_page, offset])

        usuarios = []
        for row in cur.fetchall():
            usuarios.append({
                "employeeNo": row[0],
                "name": row[1],
                "genero": row[2],
                "fecha_nacimiento": row[3].isoformat() if row[3] else "-",
                "telefono": row[4] or "-",
                "valido_hasta": row[5].isoformat() if row[5] else "-",
                "ultima_fecha": row[6].strftime('%Y-%m-%d') if row[6] else "Nunca",
                "membresia": "Vencido"
            })

        cur.close()
        conn.close()

        return render_template("usuarios_inactivos.html",
            usuarios=usuarios,
            total=total,
            page=page,
            pages=pages,
            per_page=per_page,
            busqueda=busqueda,
        )

    except Exception as e:
        return f"Error al obtener usuarios inactivos: {e}", 500







# ─── Pases del día ────────────────────────────────────────────────────────────

@app.route('/pase_diario', methods=['GET'])
def pase_diario():
    """Formulario rápido para registrar un pase del día."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT id, nombre, monto, metodo_pago, fecha
            FROM pases_diarios
            WHERE (fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
                  = (NOW() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
            ORDER BY fecha DESC
        """)
        hoy = cur.fetchall()
        cur.execute("""
            SELECT COUNT(*), COALESCE(SUM(monto), 0)
            FROM pases_diarios
            WHERE (fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
                  = (NOW() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
        """)
        hoy_count, hoy_total = cur.fetchone()
        cur.close()
        conn.close()

        zona_arg = pytz.timezone('America/Argentina/Buenos_Aires')
        pases_hoy = [
            {"id": r[0], "nombre": r[1], "monto": float(r[2]),
             "metodo_pago": r[3], "fecha": r[4].astimezone(zona_arg)}
            for r in hoy
        ]
        return render_template('pase_diario.html',
                               pases_hoy=pases_hoy,
                               hoy_count=hoy_count,
                               hoy_total=float(hoy_total))
    except Exception as e:
        return f"Error: {e}", 500


@app.route('/pase_diario', methods=['POST'])
def registrar_pase_diario():
    from pytz import timezone as tz
    nombre     = (request.form.get('nombre') or '').strip()
    monto      = request.form.get('monto', '0')
    metodo     = request.form.get('metodo_pago', '')
    notas      = (request.form.get('notas') or '').strip()

    if not nombre or not monto:
        flash('El nombre y el monto son obligatorios', 'error')
        return redirect('/pase_diario')

    try:
        ahora_arg = datetime.now(tz('America/Argentina/Buenos_Aires'))
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pases_diarios (nombre, monto, metodo_pago, fecha, notas)
            VALUES (%s, %s, %s, %s, %s)
        """, (nombre, monto, metodo or None, ahora_arg, notas or None))
        conn.commit()
        cur.close()
        conn.close()
        _log_auditoria(
            'NUEVO_PASE',
            f"{nombre} — ${monto} ({metodo or 'sin método'})" + (f" — {notas}" if notas else ""),
            ip=request.remote_addr
        )
        flash(f'✅ Pase del día registrado para {nombre}', 'success')
    except Exception as e:
        flash(f'Error al guardar: {e}', 'error')

    return redirect('/pase_diario')


@app.route('/pases_diarios')
def ver_pases_diarios():
    fecha_desde = request.args.get('desde')
    fecha_hasta = request.args.get('hasta')

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        query  = "SELECT id, nombre, monto, metodo_pago, fecha, notas FROM pases_diarios"
        params = []
        if fecha_desde and fecha_hasta:
            query += " WHERE fecha BETWEEN %s AND %s"
            params = [fecha_desde, fecha_hasta]
        elif fecha_desde:
            query += " WHERE fecha >= %s"
            params = [fecha_desde]
        elif fecha_hasta:
            query += " WHERE fecha <= %s"
            params = [fecha_hasta]
        query += " ORDER BY fecha DESC"

        cur.execute(query, params)
        rows = cur.fetchall()

        cur.execute("""
            SELECT COUNT(*), COALESCE(SUM(monto), 0) FROM pases_diarios
            WHERE (fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
                  = (NOW() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
        """)
        hoy_count, hoy_total = cur.fetchone()
        cur.close()
        conn.close()

        zona_arg = pytz.timezone('America/Argentina/Buenos_Aires')
        pases = [
            {"id": r[0], "nombre": r[1], "monto": float(r[2]),
             "metodo_pago": r[3], "fecha": r[4].astimezone(zona_arg),
             "notas": r[5]}
            for r in rows
        ]
        return render_template('pases_diarios.html',
                               pases=pases,
                               hoy_count=hoy_count,
                               hoy_total=float(hoy_total),
                               desde=fecha_desde,
                               hasta=fecha_hasta)
    except Exception as e:
        return f"Error: {e}", 500


@app.route('/pases_diarios/anular/<int:id>', methods=['POST'])
def anular_pase_diario(id):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        # Guardar datos antes de borrar para el log
        cur.execute("SELECT nombre, monto, metodo_pago FROM pases_diarios WHERE id = %s", (id,))
        row = cur.fetchone()
        detalle_log = f"ID {id}"
        if row:
            detalle_log = f"ID {id} — {row[0]} — ${row[1]} ({row[2]})"
        cur.execute("DELETE FROM pases_diarios WHERE id = %s", (id,))
        conn.commit()
        cur.close()
        conn.close()
        _log_auditoria('ANULAR_PASE', detalle_log, ip=request.remote_addr)
        flash('Pase eliminado', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'error')
    return redirect(url_for('ver_pases_diarios'))


# ─── Configuración del lector (panel UI) ──────────────────────────────────────

def _parsear_device_info(xml_text):
    """Parsea el XML de /ISAPI/System/deviceInfo tolerando namespaces."""
    from xml.etree import ElementTree as _ET
    root = _ET.fromstring(xml_text)
    def _t(tag):
        for el in root.iter():
            local = el.tag.split('}')[-1] if '}' in el.tag else el.tag
            if local == tag:
                return (el.text or '').strip() or None
        return None
    return {
        "dispositivo": _t('deviceName'),
        "modelo":      _t('model'),
        "serie":       _t('serialNumber'),
        "firmware":    _t('firmwareVersion'),
        "mac":         _t('macAddress'),
    }


@app.route('/configuracion')
def configuracion():
    cfg = _leer_config_lector()
    return render_template('configuracion.html',
                           ip=cfg['ip'],
                           user=cfg['user'],
                           pass_guardada=bool(cfg['pass']))


@app.route('/api/estado_lector')
def api_estado_lector():
    """Verifica la conexión actual con la config guardada (sin exponer credenciales)."""
    try:
        res = _hik_request(
            'GET',
            f"{BASE_URL}/System/deviceInfo",
            timeout=5,
            max_reintentos=1,
        )
        if res.status_code == 200:
            info = _parsear_device_info(res.text)
            info.update({"ok": True, "ip": HIKVISION_IP})
            return jsonify(info)
        elif res.status_code == 401:
            return jsonify({"ok": False, "error": "Credenciales incorrectas (401 Unauthorized)"})
        else:
            return jsonify({"ok": False, "error": f"Respuesta inesperada: HTTP {res.status_code}"})
    except requests.exceptions.ConnectTimeout:
        return jsonify({"ok": False, "error": f"Timeout: {HIKVISION_IP} no respondió en 5 s"})
    except requests.exceptions.ConnectionError:
        return jsonify({"ok": False, "error": f"No se pudo alcanzar {HIKVISION_IP} — verificá la red"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route('/api/probar_lector', methods=['POST'])
def api_probar_lector():
    """Prueba una IP/user/pass arbitrarios antes de guardar."""
    data     = request.get_json() or {}
    ip       = data.get('ip',       HIKVISION_IP).strip()
    user     = data.get('user',     USERNAME).strip()
    password = data.get('password', PASSWORD)

    if not ip:
        return jsonify({"ok": False, "error": "La IP no puede estar vacía"})

    try:
        res = requests.get(
            f"http://{ip}/ISAPI/System/deviceInfo",
            auth=HTTPDigestAuth(user, password),
            verify=False, timeout=5
        )
        if res.status_code == 200:
            info = _parsear_device_info(res.text)
            info.update({"ok": True, "ip": ip})
            return jsonify(info)
        elif res.status_code == 401:
            return jsonify({"ok": False, "error": "Credenciales incorrectas (401 Unauthorized)"})
        elif res.status_code == 404:
            return jsonify({"ok": False, "error": f"{ip} respondió pero no es un lector Hikvision ISAPI"})
        else:
            return jsonify({"ok": False, "error": f"Respuesta inesperada: HTTP {res.status_code}"})
    except requests.exceptions.ConnectTimeout:
        return jsonify({"ok": False, "error": f"Timeout: {ip} no respondió en 5 segundos"})
    except requests.exceptions.ConnectionError:
        return jsonify({"ok": False, "error": f"No se encontró ningún dispositivo en {ip}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route('/api/guardar_config_lector', methods=['POST'])
def api_guardar_config_lector():
    """Guarda la config en lector_config.json y la aplica en memoria al instante."""
    global HIKVISION_IP, USERNAME, PASSWORD, BASE_URL

    import json as _j
    data     = request.get_json() or {}
    nueva_ip = data.get('ip',       '').strip()
    nuevo_usr= data.get('user',     '').strip()
    nueva_pw = data.get('password', '').strip()

    if not nueva_ip or not nuevo_usr:
        return jsonify({"ok": False, "error": "La IP y el usuario son obligatorios"}), 400

    # Contraseña vacía → mantener la actual
    if not nueva_pw:
        nueva_pw = PASSWORD

    try:
        cfg_nueva = {"ip": nueva_ip, "user": nuevo_usr, "pass": nueva_pw}
        with open(_CONFIG_FILE, 'w', encoding='utf-8') as f:
            _j.dump(cfg_nueva, f, indent=2, ensure_ascii=False)

        # Aplicar en memoria sin reiniciar el servidor
        HIKVISION_IP = nueva_ip
        USERNAME     = nuevo_usr
        PASSWORD     = nueva_pw
        BASE_URL     = f"http://{HIKVISION_IP}/ISAPI"

        # Refrescar la sesión con las nuevas credenciales
        hikvision_session.auth = HTTPDigestAuth(USERNAME, PASSWORD)

        _log_auditoria(
            'CONFIG_LECTOR',
            f"IP: {nueva_ip} — Usuario: {nuevo_usr}",
            ip=request.remote_addr
        )
        return jsonify({"ok": True, "mensaje": "Configuración guardada y aplicada correctamente"})
    except Exception as e:
        return jsonify({"ok": False, "error": f"No se pudo guardar: {e}"}), 500


# ─── Auditoría ────────────────────────────────────────────────────────────────

@app.route('/auditoria')
def ver_auditoria():
    fecha_desde = request.args.get('desde')
    fecha_hasta = request.args.get('hasta')
    accion_fil  = request.args.get('accion', '')

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur  = conn.cursor()
        zona_arg = pytz.timezone('America/Argentina/Buenos_Aires')

        conds, params = [], []
        if fecha_desde:
            conds.append("(fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')::date >= %s")
            params.append(fecha_desde)
        if fecha_hasta:
            conds.append("(fecha AT TIME ZONE 'America/Argentina/Buenos_Aires')::date <= %s")
            params.append(fecha_hasta)
        if accion_fil:
            conds.append("accion = %s")
            params.append(accion_fil)

        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        cur.execute(f"""
            SELECT id, fecha, accion, detalle, ip_cliente
            FROM auditoria {where}
            ORDER BY fecha DESC
            LIMIT 500
        """, params)
        rows = cur.fetchall()

        # Acciones distintas para el filtro
        cur.execute("SELECT DISTINCT accion FROM auditoria ORDER BY accion")
        acciones = [r[0] for r in cur.fetchall()]

        cur.close()
        conn.close()

        logs = [
            {
                "id":         r[0],
                "fecha":      r[1].astimezone(zona_arg) if r[1] else None,
                "accion":     r[2],
                "detalle":    r[3],
                "ip_cliente": r[4],
            }
            for r in rows
        ]
        return render_template('auditoria.html',
                               logs=logs,
                               acciones=acciones,
                               desde=fecha_desde,
                               hasta=fecha_hasta,
                               accion_fil=accion_fil)
    except Exception as e:
        return f"Error: {e}", 500


#if __name__ == "__main__":
   # app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)
