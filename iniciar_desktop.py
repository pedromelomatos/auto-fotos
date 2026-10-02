"""Entrada do executável Windows e diagnóstico local sem acessar serviços."""

import json
import sys
from pathlib import Path

from configuracao_local import caminho_env, pasta_aplicacao
from gui import AutoFotosGUI, main


def diagnosticar(destino: Path) -> None:
    import ssl
    import tkinter as tk
    import requests
    from PIL import Image, ImageTk

    raiz = tk.Tk()
    raiz.withdraw()
    try:
        interface = AutoFotosGUI(raiz)
        # Não executa callbacks de autorização nem consultas durante o diagnóstico.
        for identificador in raiz.tk.call("after", "info"):
            raiz.after_cancel(identificador)
        raiz.update_idletasks()
        foto = ImageTk.PhotoImage(Image.new("RGB", (16, 16)), master=raiz)
        contexto = ssl.create_default_context(cafile=requests.certs.where())
        resultado = {
            "ok": True,
            "executavel": str(Path(sys.executable).resolve()),
            "pasta_aplicacao": str(pasta_aplicacao()),
            "arquivo_env": str(caminho_env()),
            "pasta_relatorios": interface.pasta_saida.get(),
            "tk": raiz.tk.call("info", "patchlevel"),
            "previa": [foto.width(), foto.height()],
            "certificados_tls": contexto.cert_store_stats()["x509_ca"],
        }
    finally:
        raiz.destroy()
    destino.write_text(json.dumps(resultado, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--diagnostico":
        saida = Path(sys.argv[2]).resolve()
        try:
            diagnosticar(saida)
        except Exception as erro:
            saida.write_text(json.dumps({"ok": False, "erro": str(erro)}), encoding="utf-8")
            raise
    else:
        main()
