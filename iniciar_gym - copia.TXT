@echo off
title ServiGym - Iniciando sistema...
REM Ir a la carpeta donde está este BAT
cd /d %~dp0

REM Crear entorno virtual si no existe
if not exist venv (
    echo Creando entorno virtual...
    python -m venv venv
    call venv\Scripts\activate
    echo Instalando dependencias...
    pip install --upgrade pip
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate
)

REM Abrir navegador automáticamente
start "" http://127.0.0.1:5000

REM Ejecutar el sistema
python app.py

pause
