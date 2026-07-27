"""Inicia o Leads Maps: sobe o servidor Flask e abre o Opera GX na pagina.
Usado pelo atalho da Area de Trabalho 'busca no Maps'.
Mantem UMA janela unica aberta (loop) ate o usuario fechar (Ctrl+C).
"""
import os
import sys
import time
import threading
import subprocess
import urllib.request

# Carrega variaveis do .env (sem dependencias externas)
BASE = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE, ".env")
if os.path.isfile(env_path):
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

OPERA = r"C:\Users\almir\AppData\Local\Programs\Opera GX\opera.exe"
URL = "http://127.0.0.1:5000"


def _server_ja_no_ar():
    try:
        urllib.request.urlopen(URL, timeout=2)
        return True
    except Exception:
        return False


def main():
    import app  # carrega o app Flask (ja le o .env internamente tambem)

    # Se ja houver um servidor na porta, nao tenta subir outro (evita 'address in use')
    if not _server_ja_no_ar():
        print("Subindo o servidor Leads Maps...")
        t = threading.Thread(target=lambda: app.app.run(port=5000, debug=False, use_reloader=False), daemon=True)
        t.start()
    else:
        print("Servidor ja estava rodando na porta 5000.")

    # Espera o servidor responder
    for i in range(15):
        if _server_ja_no_ar():
            break
        time.sleep(1)
    else:
        print("AVISO: o servidor nao respondeu em 15s. Verifique a chave GOOGLE_MAPS_API_KEY no .env.")

    # Abre o Opera GX na pagina
    if os.path.isfile(OPERA):
        subprocess.Popen([OPERA, URL])
        print(f"Abrindo o Opera GX em {URL} ...")
    else:
        import webbrowser
        webbrowser.open(URL)
        print(f"Opera GX nao encontrado; abrindo o navegador padrao em {URL} ...")

    print("\nLeads Maps ativo. Para parar, feche esta janela ou pressione Ctrl+C.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nEncerrando.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("\nOcorreu um erro. Pressione Enter para fechar.")
        input()
