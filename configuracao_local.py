"""Localiza os arquivos editáveis fora do pacote do executável."""

import sys
from pathlib import Path


def pasta_aplicacao() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def caminho_env() -> Path:
    return pasta_aplicacao() / ".env"
