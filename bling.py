"""Clientes restritos para produtos e imagens na API v3 do Bling.

O cliente base expoe somente consultas GET. A escrita fica isolada em uma
subclasse que implementa apenas o PATCH de imagens de um produto existente.
Nao ha funcoes para criar ou excluir registros.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class ErroBling(RuntimeError):
    """Erro seguro de comunicacao ou resposta da API do Bling."""


@dataclass(frozen=True, slots=True)
class ProdutoBling:
    id: int
    codigo: str
    nome: str
    situacao: str | None
    dados: dict[str, Any]

    @classmethod
    def de_resposta(cls, dados: dict[str, Any]) -> "ProdutoBling":
        try:
            identificador = int(dados["id"])
            codigo = str(dados["codigo"])
            nome = str(dados["nome"])
        except (KeyError, TypeError, ValueError) as erro:
            raise ErroBling("A API retornou um produto com formato inesperado.") from erro
        situacao = dados.get("situacao")
        return cls(
            id=identificador,
            codigo=codigo,
            nome=nome,
            situacao=str(situacao) if situacao is not None else None,
            dados=dados,
        )

    @property
    def urls_imagens(self) -> tuple[str, ...]:
        """Retorna URLs de imagens do produto sem duplicar valores."""
        midia = self.dados.get("midia")
        if not isinstance(midia, dict):
            return ()
        imagens = midia.get("imagens")
        if not isinstance(imagens, dict):
            return ()

        encontradas: list[str] = []
        vistas: set[str] = set()
        for grupo in ("externas", "internas", "imagensURL"):
            itens = imagens.get(grupo, [])
            if not isinstance(itens, list):
                continue
            for item in itens:
                candidatos: tuple[Any, ...]
                if isinstance(item, str):
                    candidatos = (item,)
                elif isinstance(item, dict):
                    candidatos = (item.get("link"), item.get("url"))
                else:
                    continue
                for candidato in candidatos:
                    if not isinstance(candidato, str):
                        continue
                    url = candidato.strip()
                    if url and url not in vistas:
                        vistas.add(url)
                        encontradas.append(url)
        return tuple(encontradas)


class BlingSomenteLeitura:
    """Cliente cuja unica operacao HTTP implementada e GET."""

    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = "https://api.bling.com.br/Api/v3",
        usar_jwt: bool = True,
        timeout: float = 30.0,
        intervalo_minimo: float = 0.36,
    ) -> None:
        if not access_token.strip():
            raise ValueError("BLING_ACCESS_TOKEN nao foi informado.")
        if not base_url.lower().startswith("https://"):
            raise ValueError("A API do Bling deve ser acessada por HTTPS.")

        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._intervalo_minimo = max(0.0, intervalo_minimo)
        self._ultima_requisicao = 0.0
        self._sessao = requests.Session()
        self._sessao.headers.update(
            {
                "Authorization": f"Bearer {access_token.strip()}",
                "Accept": "application/json",
                "User-Agent": "auto-fotos-bling/1.0",
            }
        )
        if usar_jwt:
            self._sessao.headers["enable-jwt"] = "1"

        repeticoes = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self._sessao.mount("https://", HTTPAdapter(max_retries=repeticoes))

    @classmethod
    def do_ambiente(cls) -> "BlingSomenteLeitura":
        token = os.getenv("BLING_ACCESS_TOKEN", "")
        base_url = os.getenv(
            "BLING_API_BASE_URL",
            "https://api.bling.com.br/Api/v3",
        )
        usar_jwt = os.getenv("BLING_ENABLE_JWT", "true").strip().casefold() not in {
            "0",
            "false",
            "nao",
            "não",
            "no",
        }
        return cls(token, base_url=base_url, usar_jwt=usar_jwt)

    def _get(
        self,
        caminho: str,
        *,
        params: list[tuple[str, str | int]] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}/{caminho.lstrip('/')}"
        espera = self._intervalo_minimo - (time.monotonic() - self._ultima_requisicao)
        if espera > 0:
            time.sleep(espera)
        try:
            resposta = self._sessao.get(
                url,
                params=params,
                timeout=self._timeout,
            )
        except requests.RequestException as erro:
            raise ErroBling(f"Falha ao consultar a API do Bling: {erro}") from erro
        finally:
            self._ultima_requisicao = time.monotonic()

        if resposta.status_code == 401:
            raise ErroBling("Token do Bling ausente, invalido ou expirado.")
        if resposta.status_code == 403:
            raise ErroBling("O aplicativo nao possui permissao para consultar produtos.")
        if resposta.status_code == 429:
            raise ErroBling("Limite de requisicoes do Bling atingido.")
        if not resposta.ok:
            raise ErroBling(
                f"A API do Bling respondeu com HTTP {resposta.status_code}."
            )

        try:
            conteudo = resposta.json()
        except ValueError as erro:
            raise ErroBling("A API do Bling retornou uma resposta que nao e JSON.") from erro
        if not isinstance(conteudo, dict):
            raise ErroBling("A API do Bling retornou um formato inesperado.")
        return conteudo

    def buscar_produtos_por_codigos(
        self,
        codigos: Iterable[str],
        *,
        tamanho_lote: int = 100,
    ) -> dict[str, list[ProdutoBling]]:
        """Consulta SKUs ativos em lotes e preserva possiveis duplicados ativos."""
        normalizados = sorted(
            {codigo.strip().upper() for codigo in codigos if codigo.strip()}
        )
        resultado: dict[str, list[ProdutoBling]] = {
            codigo: [] for codigo in normalizados
        }

        for inicio in range(0, len(normalizados), tamanho_lote):
            lote = normalizados[inicio : inicio + tamanho_lote]
            parametros: list[tuple[str, str | int]] = [
                ("codigos[]", codigo) for codigo in lote
            ]
            # criterio=2 limita a consulta a produtos ativos. O criterio=5
            # tambem retorna cadastros excluidos que ficam ocultos na tela
            # normal do Bling e nao devem receber imagens.
            parametros.extend((("criterio", 2), ("limite", tamanho_lote)))
            resposta = self._get("produtos", params=parametros)
            dados = resposta.get("data", [])
            if not isinstance(dados, list):
                raise ErroBling("A lista de produtos retornada possui formato inesperado.")

            for item in dados:
                if not isinstance(item, dict):
                    continue
                produto = ProdutoBling.de_resposta(item)
                chave = produto.codigo.strip().upper()
                if chave in resultado:
                    resultado[chave].append(produto)

        return resultado

    def verificar_acesso(self) -> None:
        """Confirma, com uma consulta minima, que o token acessa produtos."""
        self._get(
            "produtos",
            params=[("criterio", 2), ("limite", 1)],
        )

    def _detalhe_erro_api(self, resposta: requests.Response) -> str:
        """Extrai somente a mensagem de erro, sem expor o token usado na chamada."""
        try:
            conteudo = resposta.json()
        except ValueError:
            return ""
        erro = conteudo.get("error") if isinstance(conteudo, dict) else None
        if not isinstance(erro, dict):
            return ""
        partes: list[str] = []
        for campo in ("type", "message", "description"):
            valor = erro.get(campo)
            if isinstance(valor, str) and valor.strip() and valor.strip() not in partes:
                partes.append(valor.strip())
        detalhe = " | ".join(partes)
        token = self._sessao.headers.get("Authorization", "").removeprefix("Bearer ")
        if token:
            detalhe = detalhe.replace(token, "[token omitido]")
        return detalhe[:1000]

    def buscar_produto_por_codigo(self, codigo: str) -> ProdutoBling | None:
        encontrados = self.buscar_produtos_por_codigos([codigo]).get(
            codigo.strip().upper(),
            [],
        )
        if len(encontrados) > 1:
            raise ErroBling(f"Mais de um produto encontrado para o codigo {codigo!r}.")
        return encontrados[0] if encontrados else None

    def obter_produto(self, id_produto: int) -> ProdutoBling:
        resposta = self._get(f"produtos/{int(id_produto)}")
        dados = resposta.get("data")
        if not isinstance(dados, dict):
            raise ErroBling("Detalhes do produto retornados em formato inesperado.")
        return ProdutoBling.de_resposta(dados)


class BlingImagens(BlingSomenteLeitura):
    """Cliente com a unica escrita permitida: imagens de produto via PATCH."""

    def _patch_imagens(
        self,
        id_produto: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if int(id_produto) <= 0:
            raise ValueError("ID do produto deve ser positivo.")

        url = f"{self._base_url}/produtos/{int(id_produto)}"
        espera = self._intervalo_minimo - (time.monotonic() - self._ultima_requisicao)
        if espera > 0:
            time.sleep(espera)
        try:
            # PATCH nao usa repeticao automatica: uma resposta incerta deve ser
            # conferida com GET antes de qualquer nova tentativa.
            resposta = self._sessao.patch(
                url,
                json=payload,
                timeout=self._timeout,
            )
        except requests.RequestException as erro:
            raise ErroBling(
                "Falha ao atualizar imagens no Bling; confira o produto antes "
                "de tentar novamente."
            ) from erro
        finally:
            self._ultima_requisicao = time.monotonic()

        if resposta.status_code == 401:
            raise ErroBling("Token do Bling ausente, invalido ou expirado.")
        if resposta.status_code == 403:
            detalhe = self._detalhe_erro_api(resposta)
            raise ErroBling(
                "O Bling recusou o envio das imagens (HTTP 403)."
                + (f"\nDetalhe do Bling: {detalhe}" if detalhe else "")
            )
        if resposta.status_code == 429: #se a resposta.status for igual 429:
            raise ErroBling("Limite de requisicoes do Bling atingido.")
        if not resposta.ok: # se a resposta não estiver ok
            raise ErroBling(
                "O Bling recusou a atualizacao de imagens com HTTP "
                f"{resposta.status_code}."
            )

        if not resposta.content:
            return {}
        try:
            conteudo = resposta.json()
        except ValueError as erro:
            raise ErroBling(
                "A resposta da atualizacao de imagens nao e JSON valido."
            ) from erro
        if not isinstance(conteudo, dict):
            raise ErroBling("A resposta da atualizacao de imagens e inesperada.")
        return conteudo

    def atualizar_imagens_produto(
        self,
        id_produto: int,
        urls: Iterable[str],
    ) -> dict[str, Any]:
        """Substitui a lista remota pela lista completa, validada e sem duplicatas."""
        finais: list[str] = []
        vistas: set[str] = set()
        for valor in urls:
            url = str(valor).strip()
            partes = urlsplit(url)
            if partes.scheme.casefold() not in {"http", "https"} or not partes.netloc:
                raise ValueError(f"URL de imagem invalida: {url!r}")
            if url not in vistas:
                vistas.add(url)
                finais.append(url)

        if not finais:
            raise ValueError(
                "A lista final de imagens nao pode ser vazia; a operacao foi bloqueada."
            )

        payload = {
            "midia": {
                "imagens": {
                    "imagensURL": [{"link": url} for url in finais],
                }
            }
        }
        return self._patch_imagens(id_produto, payload)
