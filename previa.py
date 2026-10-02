"""Carregamento limitado de fotos para a prévia, sem dependência de Tkinter."""

from io import BytesIO
import time
import warnings

import requests
from PIL import Image, ImageOps


LIMITE_BYTES = 12 * 1024 * 1024
LIMITE_PIXELS = 24_000_000


def carregar_previa(url: str, verificar_certificado: bool) -> Image.Image:
    """Baixa e reduz uma foto em memória; não grava arquivos nem envia dados."""
    inicio = time.monotonic()
    with requests.get(
        url, stream=True, timeout=(3, 8), verify=verificar_certificado
    ) as resposta:
        resposta.raise_for_status()
        dados = bytearray()
        for bloco in resposta.iter_content(64 * 1024):
            dados.extend(bloco)
            if len(dados) > LIMITE_BYTES:
                raise ValueError("Imagem muito grande para a prévia.")
            if time.monotonic() - inicio > 15:
                raise TimeoutError("A imagem demorou demais para carregar.")

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(dados)) as original:
            if original.width * original.height > LIMITE_PIXELS:
                raise ValueError("Imagem muito grande para a prévia.")
            imagem = ImageOps.exif_transpose(original)
            imagem.thumbnail((520, 320), Image.Resampling.LANCZOS)
            return imagem.convert("RGBA")
