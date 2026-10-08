"""Coleta e organiza links de imagens do servidor intermediario.

Este modulo nao se comunica com o Bling. Ele apenas le um indice HTML,
identifica imagens pelo nome do arquivo e gera dados para conferencia.
"""

from __future__ import annotations

import csv
import logging
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from html import unescape
from pathlib import PurePosixPath
from typing import Iterable
from urllib.parse import unquote, urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from urllib3.exceptions import InsecureRequestWarning


LOGGER = logging.getLogger(__name__)

EXTENSOES_ACEITAS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
SUBSTITUICOES_CODIGO_BLING = {
    # O servidor usa PR31597, mas o SKU correspondente no Bling usa RE.
    "PR31597": "RE315970001",
}
PADRAO_NOME_IMAGEM = re.compile(
    r"^(?P<codigo>(?:PR|RE|MLB)\d+)(?=[^A-Z0-9]|$).*_(?P<posicao>\d+)"
    r"(?P<extensao>\.(?:jpe?g|png|webp))$",
    re.IGNORECASE,
)
PADRAO_NOME_SEM_POSICAO = re.compile(
    r"^(?P<codigo>(?:PR|RE|MLB)\d+)(?=[^A-Z0-9]|$).*"
    r"\.(?:jpe?g|png|webp)$",
    re.IGNORECASE,
)


class ErroServidorImagens(RuntimeError):
    """Falha ao acessar ou interpretar o servidor de imagens."""


@dataclass(frozen=True, slots=True)
class ImagemProduto:
    codigo_servidor: str
    codigo_bling: str
    posicao: int
    arquivo: str
    url: str
    posicao_automatica: bool = False


@dataclass(slots=True)
class ProdutoImagens:
    codigo_servidor: str
    codigo_bling: str
    imagens: list[ImagemProduto] = field(default_factory=list)

    def adicionar(self, imagem: ImagemProduto) -> None:
        if imagem.codigo_servidor != self.codigo_servidor:
            raise ValueError("A imagem pertence a outro produto.")
        if any(item.url == imagem.url for item in self.imagens):
            return
        self.imagens.append(imagem)

    def ordenar(self) -> None:
        # Nomes descritivos nao possuem _01: recebem posicoes estaveis depois
        # das numeradas, sem mascarar faltas ou conflitos nas posicoes explicitas.
        proxima = max(
            (item.posicao for item in self.imagens if not item.posicao_automatica),
            default=0,
        ) + 1
        automaticas = sorted(
            (item for item in self.imagens if item.posicao_automatica),
            key=lambda item: (item.arquivo.casefold(), item.url),
        )
        posicoes = {item.url: proxima + indice for indice, item in enumerate(automaticas)}
        self.imagens = [
            replace(item, posicao=posicoes[item.url]) if item.posicao_automatica else item
            for item in self.imagens
        ]
        self.imagens.sort(key=lambda item: (item.posicao, item.arquivo.casefold()))

    @property
    def posicoes(self) -> list[int]:
        return sorted({imagem.posicao for imagem in self.imagens})

    @property
    def posicoes_faltantes(self) -> list[int]:
        if not self.imagens:
            return []
        encontradas = set(self.posicoes)
        return [numero for numero in range(1, max(encontradas) + 1) if numero not in encontradas]

    @property
    def posicoes_em_conflito(self) -> list[int]:
        contagem = Counter(imagem.posicao for imagem in self.imagens)
        return sorted(posicao for posicao, total in contagem.items() if total > 1)

    @property
    def status_sequencia(self) -> str:
        if self.posicoes_em_conflito:
            return "CONFLITO_POSICAO"
        if self.posicoes_faltantes:
            return "NUMEROS_FALTANTES"
        return "OK"


@dataclass(frozen=True, slots=True)
class ResultadoColeta:
    produtos: tuple[ProdutoImagens, ...]
    links_examinados: int
    arquivos_imagem_ignorados: tuple[str, ...]

    @property
    def total_produtos(self) -> int:
        return len(self.produtos)

    @property
    def total_imagens(self) -> int:
        return sum(len(produto.imagens) for produto in self.produtos)


@dataclass(frozen=True, slots=True)
class DiretorioImagens:
    nome: str
    url: str


def codigo_bling(codigo_servidor: str) -> str:
    """Converte PR/RE com sufixo e preserva SKUs MLB, incluindo excecoes."""
    codigo = codigo_servidor.strip().upper()
    if not re.fullmatch(r"(?:PR|RE|MLB)\d+", codigo):
        raise ValueError(f"Codigo de produto invalido: {codigo_servidor!r}")
    if codigo in SUBSTITUICOES_CODIGO_BLING:
        return SUBSTITUICOES_CODIGO_BLING[codigo]
    if codigo.startswith("MLB"):
        return codigo
    return f"{codigo}0001"


def analisar_nome_arquivo(arquivo: str, url: str) -> ImagemProduto | None:
    """Interpreta um nome de imagem; retorna None quando ele nao segue o padrao."""
    nome = unquote(PurePosixPath(arquivo).name)
    correspondencia = PADRAO_NOME_IMAGEM.match(nome)
    posicao_automatica = correspondencia is None
    if posicao_automatica:
        correspondencia = PADRAO_NOME_SEM_POSICAO.match(nome)
    if correspondencia is None:
        return None

    codigo = correspondencia.group("codigo").upper()
    posicao = 1 if posicao_automatica else int(correspondencia.group("posicao"))
    if posicao < 1:
        return None

    return ImagemProduto(
        codigo_servidor=codigo,
        codigo_bling=codigo_bling(codigo),
        posicao=posicao,
        arquivo=nome,
        url=url,
        posicao_automatica=posicao_automatica,
    )


def _mesma_origem_e_diretorio(base_url: str, destino: str) -> bool:
    base = urlparse(base_url)
    alvo = urlparse(destino)
    caminho_base = base.path if base.path.endswith("/") else f"{base.path}/"
    return (
        alvo.scheme in {"http", "https"}
        and alvo.scheme.casefold() == base.scheme.casefold()
        and alvo.netloc.casefold() == base.netloc.casefold()
        and alvo.path.startswith(caminho_base)
    )


def extrair_links_imagens(html: str, base_url: str) -> tuple[list[str], list[str], int]:
    """Extrai links validos e lista arquivos de imagem fora do padrao esperado."""
    sopa = BeautifulSoup(html, "html.parser")
    imagens: list[str] = []
    ignorados: list[str] = []
    vistos: set[str] = set()
    links_examinados = 0

    for ancora in sopa.find_all("a", href=True):
        links_examinados += 1
        href = unescape(str(ancora["href"]).strip())
        if not href or href.startswith(("#", "?")):
            continue

        url = requests.utils.requote_uri(urljoin(base_url, href))
        if not _mesma_origem_e_diretorio(base_url, url):
            LOGGER.debug("Link externo ou fora do diretorio ignorado: %s", url)
            continue

        caminho = urlparse(url).path
        arquivo = unquote(PurePosixPath(caminho).name)
        if PurePosixPath(arquivo).suffix.casefold() not in EXTENSOES_ACEITAS:
            continue

        if url in vistos:
            continue
        vistos.add(url)

        if analisar_nome_arquivo(arquivo, url) is None:
            ignorados.append(arquivo)
        else:
            imagens.append(url)

    return imagens, ignorados, links_examinados


def extrair_diretorios(html: str, base_url: str) -> tuple[DiretorioImagens, ...]:
    """Extrai somente os subdiretorios imediatos publicados no indice HTML."""
    if not base_url.endswith("/"):
        base_url = f"{base_url}/"
    base = urlparse(base_url)
    caminho_base = base.path
    sopa = BeautifulSoup(html, "html.parser")
    encontrados: dict[str, DiretorioImagens] = {}

    for ancora in sopa.find_all("a", href=True):
        href = unescape(str(ancora["href"]).strip())
        if not href or href.startswith(("#", "?")):
            continue
        url = requests.utils.requote_uri(urljoin(base_url, href))
        alvo = urlparse(url)
        if (
            alvo.scheme.casefold() != base.scheme.casefold()
            or alvo.netloc.casefold() != base.netloc.casefold()
            or not alvo.path.endswith("/")
            or not alvo.path.startswith(caminho_base)
        ):
            continue

        relativo = unquote(alvo.path[len(caminho_base) :]).strip("/")
        if not relativo or "/" in relativo or relativo in {".", ".."}:
            continue
        chave = relativo.casefold()
        encontrados.setdefault(chave, DiretorioImagens(relativo, url))

    return tuple(sorted(encontrados.values(), key=lambda item: item.nome.casefold()))


def listar_diretorios(
    base_url: str,
    *,
    verificar_certificado: bool = False,
    timeout: float = 30.0,
    sessao: requests.Session | None = None,
) -> tuple[DiretorioImagens, ...]:
    """Consulta a raiz do servidor e retorna as pastas selecionaveis."""
    if not base_url.endswith("/"):
        base_url = f"{base_url}/"
    cliente = sessao or requests.Session()
    cliente.headers.setdefault("User-Agent", "auto-fotos-bling/1.0")

    if not verificar_certificado:
        urllib3.disable_warnings(InsecureRequestWarning)

    try:
        resposta = cliente.get(
            base_url,
            timeout=timeout,
            verify=verificar_certificado,
        )
        resposta.raise_for_status()
    except requests.RequestException as erro:
        raise ErroServidorImagens(
            f"Nao foi possivel listar as pastas do servidor: {erro}"
        ) from erro

    return extrair_diretorios(resposta.text, base_url)


def coletar_imagens(
    base_url: str,
    *,
    verificar_certificado: bool = False,
    timeout: float = 30.0,
    sessao: requests.Session | None = None,
) -> ResultadoColeta:
    """Le o diretorio remoto e agrupa as imagens por codigo de produto."""
    if not base_url.endswith("/"):
        base_url = f"{base_url}/"

    cliente = sessao or requests.Session()
    cliente.headers.setdefault("User-Agent", "auto-fotos-bling/1.0")

    if not verificar_certificado:
        urllib3.disable_warnings(InsecureRequestWarning)
        LOGGER.warning(
            "Validacao TLS desativada somente para o servidor de imagens. "
            "Corrija o certificado do servidor assim que possivel."
        )

    try:
        resposta = cliente.get(
            base_url,
            timeout=timeout,
            verify=verificar_certificado,
        )
        resposta.raise_for_status()
    except requests.RequestException as erro:
        raise ErroServidorImagens(
            f"Nao foi possivel acessar o servidor de imagens: {erro}"
        ) from erro

    links, ignorados, links_examinados = extrair_links_imagens(
        resposta.text,
        base_url,
    )

    agrupados: dict[str, ProdutoImagens] = {}
    for url in links:
        arquivo = unquote(PurePosixPath(urlparse(url).path).name)
        imagem = analisar_nome_arquivo(arquivo, url)
        if imagem is None:  # Protecao adicional; a filtragem ja ocorreu acima.
            continue
        produto = agrupados.setdefault(
            imagem.codigo_servidor,
            ProdutoImagens(imagem.codigo_servidor, imagem.codigo_bling),
        )
        produto.adicionar(imagem)

    produtos = sorted(agrupados.values(), key=lambda item: item.codigo_servidor)
    for produto in produtos:
        produto.ordenar()

    return ResultadoColeta(
        produtos=tuple(produtos),
        links_examinados=links_examinados,
        arquivos_imagem_ignorados=tuple(sorted(ignorados, key=str.casefold)),
    )


def _formatar_numeros(numeros: Iterable[int]) -> str:
    return ",".join(f"{numero:02d}" for numero in numeros)


def gerar_csv(resultado: ResultadoColeta, caminho: str = "imagens_produtos.csv") -> None:
    """Gera um CSV detalhado, compativel com Excel em Windows."""
    colunas = [
        "codigo_servidor",
        "codigo_bling",
        "status_sequencia",
        "quantidade_imagens",
        "posicoes_encontradas",
        "posicoes_faltantes",
        "posicoes_em_conflito",
        "posicao",
        "arquivo",
        "url",
    ]

    with open(caminho, "w", newline="", encoding="utf-8-sig") as arquivo_csv:
        escritor = csv.DictWriter(arquivo_csv, fieldnames=colunas, delimiter=";")
        escritor.writeheader()
        for produto in resultado.produtos:
            dados_produto = {
                "codigo_servidor": produto.codigo_servidor,
                "codigo_bling": produto.codigo_bling,
                "status_sequencia": produto.status_sequencia,
                "quantidade_imagens": len(produto.imagens),
                "posicoes_encontradas": _formatar_numeros(produto.posicoes),
                "posicoes_faltantes": _formatar_numeros(produto.posicoes_faltantes),
                "posicoes_em_conflito": _formatar_numeros(produto.posicoes_em_conflito),
            }
            for imagem in produto.imagens:
                escritor.writerow(
                    {
                        **dados_produto,
                        "posicao": f"{imagem.posicao:02d}",
                        "arquivo": imagem.arquivo,
                        "url": imagem.url,
                    }
                )
