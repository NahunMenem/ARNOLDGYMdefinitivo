import subprocess
import platform

# Rango de red a escanear (ajustar si tu red no es 192.168.1.x)
base_ip = "192.168.1."
sistema = platform.system()

print("🔍 Escaneando red para encontrar dispositivos activos...\n")

for i in range(1, 255):
    ip = f"{base_ip}{i}"
    if sistema == "Windows":
        comando = ["ping", "-n", "1", "-w", "200", ip]  # 200 ms timeout
    else:
        comando = ["ping", "-c", "1", "-W", "200", ip]

    try:
        resultado = subprocess.run(comando, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if resultado.returncode == 0:
            print(f"✅ Dispositivo activo: {ip}")
    except Exception as e:
        print(f"Error con {ip}: {e}")

print("\n✅ Escaneo finalizado.")
print("📌 Revisa las IPs activas y probá en el navegador si es el lector Hikvision.")
