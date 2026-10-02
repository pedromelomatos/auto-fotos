"""Gera o executável único e um ZIP sem credenciais locais."""

from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

import PyInstaller.__main__


def main() -> None:
    raiz = Path(__file__).resolve().parent
    destino = raiz / "dist" / "windows"
    trabalho = raiz / "build" / "windows"
    trabalho.mkdir(parents=True, exist_ok=True)
    PyInstaller.__main__.run([
        str(raiz / "iniciar_desktop.py"),
        "--name", "AutoFotos",
        "--onefile", "--windowed", "--noconfirm", "--noupx",
        "--distpath", str(destino),
        "--workpath", str(trabalho / "pyinstaller"),
        "--specpath", str(trabalho),
    ])
    guia = raiz / "docs" / "LEIA-ME-executavel.txt"
    zip_path = raiz / "dist" / "auto-fotos-windows.zip"
    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as pacote:
        pacote.write(destino / "AutoFotos.exe", "AutoFotos/AutoFotos.exe")
        pacote.write(raiz / ".env.example", "AutoFotos/.env")
        pacote.write(guia, "AutoFotos/LEIA-ME.txt")
    with ZipFile(zip_path) as pacote:
        assert pacote.testzip() is None
        assert len(pacote.namelist()) == 3
    print(f"Pacote gerado: {zip_path}")


if __name__ == "__main__":
    main()
