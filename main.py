"""Entrada de linha de comando da automacao de imagens para o Bling."""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass, replace
from urllib.parse import unquote, urlsplit, urlunsplit

from dotenv import load_dotenv

from bling import BlingImagens, BlingSomenteLeitura, ErroBling, ProdutoBling
from servidor import (
    ErroServidorImagens,
    ProdutoImagens,
    ResultadoColeta,
    coletar_imagens,
    gerar_csv,
)


URL_PADRAO = "https://servidor.exemplo/FABRICANTE/"
CONFIRMACAO_ESCRITA = "APLICAR"


@dataclass(frozen=True, slots=True)
class ResultadoAplicacao:
    codigo_servidor: str
    codigo_bling: str
    id_bling: int | None
    status: str
    motivo: str
    quantidade_antes: int
    quantidade_novas: int
    quantidade_depois: int


def valor_booleano(valor: str | None, *, padrao: bool = False) -> bool:
    if valor is None:
        return padrao
    return valor.strip().casefold() in {"1", "true", "sim", "yes", "on"}


def criar_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Coleta imagens por SKU e, opcionalmente, confere produtos no Bling.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="URL do diretorio do fabricante (padrao: IMAGENS_BASE_URL).",
    )
    parser.add_argument(
        "--saida",
        default="imagens_produtos.csv",
        help="CSV de imagens gerado.",
    )
    parser.add_argument(
        "--consultar-bling",
        action="store_true",
        help="Consulta os SKUs no Bling usando somente GET.",
    )
    parser.add_argument(
        "--saida-bling",
        default="conferencia_bling.csv",
        help="CSV da conferencia somente leitura no Bling.",
    )
    parser.add_argument(
        "--simular-imagens",
        action="store_true",
        help="Compara imagens existentes e gera um plano sem alterar o Bling.",
    )
    parser.add_argument(
        "--saida-simulacao",
        default="plano_imagens.csv",
        help="CSV do plano de imagens gerado pelo modo de simulacao.",
    )
    parser.add_argument(
        "--aplicar-imagens",
        action="store_true",
        help="Aplica imagens em um unico SKU piloto e confere o resultado.",
    )
    parser.add_argument(
        "--sku-piloto",
        default=None,
        help="SKU completo do Bling autorizado para a aplicacao piloto.",
    )
    parser.add_argument(
        "--confirmar-escrita",
        default=None,
        help=f"Confirmacao obrigatoria para escrita: {CONFIRMACAO_ESCRITA}.",
    )
    parser.add_argument(
        "--saida-aplicacao",
        default="aplicacao_imagens.csv",
        help="CSV de auditoria da aplicacao piloto.",
    )
    parser.add_argument(
        "--validar-certificado-servidor",
        action="store_true",
        help="Exige certificado TLS valido no servidor de imagens.",
    )
    return parser


def validar_configuracao_escrita(argumentos: argparse.Namespace) -> None:
    """Bloqueia escrita sem as duas confirmacoes explicitas do operador."""
    if not argumentos.aplicar_imagens:
        return
    sku = str(argumentos.sku_piloto or "").strip().upper()
    if not sku:
        raise ValueError("--aplicar-imagens exige --sku-piloto com o SKU completo.")
    if argumentos.confirmar_escrita != CONFIRMACAO_ESCRITA:
        raise ValueError(
            "--aplicar-imagens exige --confirmar-escrita "
            f"{CONFIRMACAO_ESCRITA}."
        )
    argumentos.sku_piloto = sku


def gerar_conferencia_bling(
    resultado: ResultadoColeta,
    encontrados: dict[str, list[ProdutoBling]],
    caminho: str,
) -> None:
    colunas = [
        "codigo_servidor",
        "codigo_bling_consultado",
        "status_consulta",
        "id_bling",
        "codigo_bling_retornado",
        "nome_bling",
        "situacao_bling",
        "quantidade_imagens_servidor",
        "status_sequencia_imagens",
    ]
    with open(caminho, "w", newline="", encoding="utf-8-sig") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=colunas, delimiter=";")
        escritor.writeheader()
        for produto in resultado.produtos:
            correspondencias = encontrados.get(produto.codigo_bling, [])
            if len(correspondencias) == 1:
                produto_bling = correspondencias[0]
                status = "ENCONTRADO"
            elif len(correspondencias) > 1:
                produto_bling = correspondencias[0]
                status = "CONFLITO_MULTIPLOS_PRODUTOS"
            else:
                produto_bling = None
                status = "NAO_ENCONTRADO"

            escritor.writerow(
                {
                    "codigo_servidor": produto.codigo_servidor,
                    "codigo_bling_consultado": produto.codigo_bling,
                    "status_consulta": status,
                    "id_bling": produto_bling.id if produto_bling else "",
                    "codigo_bling_retornado": produto_bling.codigo if produto_bling else "",
                    "nome_bling": produto_bling.nome if produto_bling else "",
                    "situacao_bling": produto_bling.situacao if produto_bling else "",
                    "quantidade_imagens_servidor": len(produto.imagens),
                    "status_sequencia_imagens": produto.status_sequencia,
                }
            )


def resolver_produtos_bling(
    resultado: ResultadoColeta,
    cliente: BlingSomenteLeitura,
) -> tuple[ResultadoColeta, dict[str, list[ProdutoBling]]]:
    """Tenta a regra tradicional e depois o codigo exato presente no arquivo."""
    codigos = sorted({produto.codigo_bling for produto in resultado.produtos})
    encontrados = cliente.buscar_produtos_por_codigos(codigos)
    alternativas = sorted({
        produto.codigo_servidor
        for produto in resultado.produtos
        if not encontrados.get(produto.codigo_bling)
        and produto.codigo_servidor != produto.codigo_bling
    })
    # Reutiliza os resultados da primeira consulta quando o codigo original
    # tambem era candidato de outro grupo. Nunca troca uma correspondencia existente.
    nao_consultados = [codigo for codigo in alternativas if codigo not in encontrados]
    if nao_consultados:
        encontrados.update(cliente.buscar_produtos_por_codigos(nao_consultados))

    agrupados: dict[str, ProdutoImagens] = {}
    for produto in resultado.produtos:
        codigo = produto.codigo_bling
        if not encontrados.get(codigo) and encontrados.get(produto.codigo_servidor):
            codigo = produto.codigo_servidor
        destino = agrupados.setdefault(
            codigo, ProdutoImagens(produto.codigo_servidor, codigo),
        )
        urls = {imagem.url for imagem in destino.imagens}
        for imagem in produto.imagens:
            if imagem.url not in urls:
                # Mantem o codigo de origem de cada imagem para auditoria, inclusive
                # quando nomes abreviados e completos apontam para o mesmo SKU.
                destino.imagens.append(replace(imagem, codigo_bling=codigo))
                urls.add(imagem.url)
        destino.ordenar()

    return replace(resultado, produtos=tuple(agrupados.values())), encontrados


def normalizar_url_imagem(url: str) -> str:
    """Normaliza URL para comparacao sem alterar o valor gravado no relatorio."""
    partes = urlsplit(url.strip())
    return urlunsplit(
        (
            partes.scheme.casefold(),
            partes.netloc.casefold(),
            unquote(partes.path),
            partes.query,
            "",
        )
    )


def criar_plano_imagens(
    resultado: ResultadoColeta,
    encontrados: dict[str, list[ProdutoBling]],
    cliente: BlingSomenteLeitura,
) -> list[dict[str, str | int]]:
    """Cria um plano somente leitura, com uma linha para cada imagem do servidor."""
    linhas: list[dict[str, str | int]] = []

    for produto in resultado.produtos:
        correspondencias = encontrados.get(produto.codigo_bling, [])
        detalhe: ProdutoBling | None = None
        motivo_bloqueio = ""

        if produto.status_sequencia != "OK":
            status = "BLOQUEADO_SEQUENCIA_INCONSISTENTE"
            motivo_bloqueio = produto.status_sequencia
        elif not correspondencias:
            status = "IGNORADO_PRODUTO_NAO_ENCONTRADO"
            motivo_bloqueio = (
                "Nenhum produto ativo encontrado; imagem ignorada e "
                "processamento continuado."
            )
        elif len(correspondencias) > 1:
            status = "BLOQUEADO_MULTIPLOS_PRODUTOS_ATIVOS"
            motivo_bloqueio = "Mais de um produto ativo possui o mesmo codigo."
        else:
            resumo = correspondencias[0]
            try:
                detalhe = cliente.obter_produto(resumo.id)
            except ErroBling as erro:
                status = "BLOQUEADO_ERRO_AO_CONSULTAR_DETALHES"
                motivo_bloqueio = str(erro)
            else:
                if detalhe.codigo.strip().upper() != produto.codigo_bling:
                    status = "BLOQUEADO_CODIGO_DIVERGENTE"
                    motivo_bloqueio = "O codigo retornado nos detalhes diverge do consultado."
                elif detalhe.situacao != "A":
                    status = "BLOQUEADO_PRODUTO_NAO_ATIVO"
                    motivo_bloqueio = f"Situacao retornada: {detalhe.situacao or 'vazia'}."
                else:
                    status = "PRONTO_PARA_COMPARAR"

        urls_existentes = detalhe.urls_imagens if detalhe is not None else ()
        urls_normalizadas = {normalizar_url_imagem(url) for url in urls_existentes}

        for imagem in produto.imagens:
            status_imagem = status
            motivo = motivo_bloqueio
            if status == "PRONTO_PARA_COMPARAR":
                if normalizar_url_imagem(imagem.url) in urls_normalizadas:
                    status_imagem = "JA_EXISTE"
                    motivo = "A mesma URL ja esta cadastrada no produto ativo."
                else:
                    status_imagem = "ADICIONAR"
                    motivo = "URL nova; seria adicionada preservando as imagens existentes."

            linhas.append(
                {
                    "codigo_servidor": produto.codigo_servidor,
                    "codigo_bling": produto.codigo_bling,
                    "id_bling": detalhe.id if detalhe is not None else "",
                    "nome_bling": detalhe.nome if detalhe is not None else "",
                    "situacao_bling": detalhe.situacao if detalhe is not None else "",
                    "posicao": f"{imagem.posicao:02d}",
                    "url_servidor": imagem.url,
                    "status_planejado": status_imagem,
                    "motivo": motivo,
                    "quantidade_imagens_existentes": len(urls_existentes),
                    "urls_existentes_preservadas": " | ".join(urls_existentes),
                }
            )

    return linhas


def gerar_plano_imagens(
    linhas: list[dict[str, str | int]],
    caminho: str = "plano_imagens.csv",
) -> None:
    colunas = [
        "codigo_servidor",
        "codigo_bling",
        "id_bling",
        "nome_bling",
        "situacao_bling",
        "posicao",
        "url_servidor",
        "status_planejado",
        "motivo",
        "quantidade_imagens_existentes",
        "urls_existentes_preservadas",
    ]
    with open(caminho, "w", newline="", encoding="utf-8-sig") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=colunas, delimiter=";")
        escritor.writeheader()
        escritor.writerows(linhas)


def aplicar_imagens_piloto(
    resultado: ResultadoColeta,
    encontrados: dict[str, list[ProdutoBling]],
    cliente: BlingImagens,
    codigo_piloto: str,
) -> ResultadoAplicacao:
    """Aplica somente um SKU, preserva URLs existentes e verifica com novo GET."""
    codigo = codigo_piloto.strip().upper()
    produtos = [produto for produto in resultado.produtos if produto.codigo_bling == codigo]
    if len(produtos) != 1:
        raise ValueError(
            f"O SKU piloto {codigo!r} deve corresponder a exatamente um item do servidor."
        )
    produto = produtos[0]
    correspondencias = encontrados.get(codigo, [])

    if produto.status_sequencia != "OK":
        return ResultadoAplicacao(
            produto.codigo_servidor,
            codigo,
            None,
            "BLOQUEADO_SEQUENCIA_INCONSISTENTE",
            produto.status_sequencia,
            0,
            0,
            0,
        )
    if not correspondencias:
        return ResultadoAplicacao(
            produto.codigo_servidor,
            codigo,
            None,
            "IGNORADO_PRODUTO_NAO_ENCONTRADO",
            "Nenhum produto ativo encontrado; nenhuma escrita foi realizada.",
            0,
            0,
            0,
        )
    if len(correspondencias) > 1:
        return ResultadoAplicacao(
            produto.codigo_servidor,
            codigo,
            None,
            "BLOQUEADO_MULTIPLOS_PRODUTOS_ATIVOS",
            "Mais de um produto ativo possui o SKU piloto.",
            0,
            0,
            0,
        )

    resumo = correspondencias[0]
    antes = cliente.obter_produto(resumo.id)
    if antes.codigo.strip().upper() != codigo:
        return ResultadoAplicacao(
            produto.codigo_servidor,
            codigo,
            antes.id,
            "BLOQUEADO_CODIGO_DIVERGENTE",
            "O codigo retornado antes da escrita diverge do SKU piloto.",
            len(antes.urls_imagens),
            0,
            len(antes.urls_imagens),
        )
    if antes.situacao != "A":
        return ResultadoAplicacao(
            produto.codigo_servidor,
            codigo,
            antes.id,
            "BLOQUEADO_PRODUTO_NAO_ATIVO",
            f"Situacao retornada: {antes.situacao or 'vazia'}.",
            len(antes.urls_imagens),
            0,
            len(antes.urls_imagens),
        )

    urls_antes = list(antes.urls_imagens)
    normalizadas = {normalizar_url_imagem(url) for url in urls_antes}
    urls_novas: list[str] = []
    for imagem in produto.imagens:
        normalizada = normalizar_url_imagem(imagem.url)
        if normalizada not in normalizadas:
            normalizadas.add(normalizada)
            urls_novas.append(imagem.url)

    if not urls_novas:
        return ResultadoAplicacao(
            produto.codigo_servidor,
            codigo,
            antes.id,
            "SEM_ALTERACAO",
            "Todas as imagens do servidor ja existem no produto.",
            len(urls_antes),
            0,
            len(urls_antes),
        )

    urls_finais = [*urls_antes, *urls_novas]
    cliente.atualizar_imagens_produto(antes.id, urls_finais)

    depois = cliente.obter_produto(antes.id)
    urls_depois_normalizadas = {
        normalizar_url_imagem(url) for url in depois.urls_imagens
    }
    faltantes = [
        url
        for url in urls_finais
        if normalizar_url_imagem(url) not in urls_depois_normalizadas
    ]
    if faltantes:
        return ResultadoAplicacao(
            produto.codigo_servidor,
            codigo,
            antes.id,
            "ERRO_VERIFICACAO_POS_PATCH",
            f"{len(faltantes)} URL(s) esperada(s) nao apareceram na conferencia.",
            len(urls_antes),
            len(urls_novas),
            len(depois.urls_imagens),
        )

    return ResultadoAplicacao(
        produto.codigo_servidor,
        codigo,
        antes.id,
        "APLICADO_E_VERIFICADO",
        "PATCH concluido e imagens confirmadas por consulta posterior.",
        len(urls_antes),
        len(urls_novas),
        len(depois.urls_imagens),
    )


def gerar_relatorio_aplicacao(
    resultado: ResultadoAplicacao,
    caminho: str = "aplicacao_imagens.csv",
) -> None:
    colunas = [
        "codigo_servidor",
        "codigo_bling",
        "id_bling",
        "status",
        "motivo",
        "quantidade_antes",
        "quantidade_novas",
        "quantidade_depois",
    ]
    with open(caminho, "w", newline="", encoding="utf-8-sig") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=colunas, delimiter=";")
        escritor.writeheader()
        escritor.writerow(
            {
                "codigo_servidor": resultado.codigo_servidor,
                "codigo_bling": resultado.codigo_bling,
                "id_bling": resultado.id_bling or "",
                "status": resultado.status,
                "motivo": resultado.motivo,
                "quantidade_antes": resultado.quantidade_antes,
                "quantidade_novas": resultado.quantidade_novas,
                "quantidade_depois": resultado.quantidade_depois,
            }
        )


def executar(argumentos: argparse.Namespace) -> int:
    load_dotenv()
    validar_configuracao_escrita(argumentos)
    url = argumentos.url or os.getenv("IMAGENS_BASE_URL", URL_PADRAO)
    validar_certificado = argumentos.validar_certificado_servidor or valor_booleano(
        os.getenv("SERVIDOR_VERIFY_SSL"),
        padrao=False,
    )

    resultado = coletar_imagens(
        url,
        verificar_certificado=validar_certificado,
    )
    gerar_csv(resultado, argumentos.saida)

    print(f"Produtos encontrados: {resultado.total_produtos}")
    print(f"Imagens encontradas: {resultado.total_imagens}")
    print(f"CSV gerado: {argumentos.saida}")
    if resultado.arquivos_imagem_ignorados:
        print(
            "Imagens ignoradas por nome fora do padrao: "
            f"{len(resultado.arquivos_imagem_ignorados)}"
        )

    inconsistentes = [
        produto
        for produto in resultado.produtos
        if produto.status_sequencia != "OK"
    ]
    if inconsistentes:
        print(f"Produtos com sequencia inconsistente: {len(inconsistentes)}")

    if (
        argumentos.consultar_bling
        or argumentos.simular_imagens
        or argumentos.aplicar_imagens
    ):
        cliente: BlingSomenteLeitura
        if argumentos.aplicar_imagens:
            cliente = BlingImagens.do_ambiente()
        else:
            cliente = BlingSomenteLeitura.do_ambiente()
        resultado, encontrados = resolver_produtos_bling(resultado, cliente)
        gerar_csv(resultado, argumentos.saida)
        codigos = [produto.codigo_bling for produto in resultado.produtos]
        total_com_correspondencia = sum(
            1 for codigo in codigos if encontrados.get(codigo, [])
        )
        total_unicos = sum(
            1 for codigo in codigos if len(encontrados.get(codigo, [])) == 1
        )
        total_conflitos = sum(
            1 for codigo in codigos if len(encontrados.get(codigo, [])) > 1
        )
        produtos_nao_encontrados = [
            produto
            for produto in resultado.produtos
            if not encontrados.get(produto.codigo_bling, [])
        ]
        total_nao_encontrados = len(produtos_nao_encontrados)
        if produtos_nao_encontrados:
            codigos_ignorados = ", ".join(
                produto.codigo_bling for produto in produtos_nao_encontrados
            )
            print(
                "AVISO: produtos ativos nao encontrados; imagens ignoradas e "
                f"processamento continuado: {codigos_ignorados}"
            )
        if argumentos.consultar_bling:
            gerar_conferencia_bling(resultado, encontrados, argumentos.saida_bling)
            print(
                "Produtos com ao menos uma correspondencia no Bling: "
                f"{total_com_correspondencia}/{len(codigos)}"
            )
            print(f"Correspondencia unica: {total_unicos}")
            print(f"Multiplos cadastros com o mesmo codigo: {total_conflitos}")
            print(f"Nao encontrados: {total_nao_encontrados}")
            print(f"Conferencia Bling gerada: {argumentos.saida_bling}")

        if argumentos.simular_imagens or argumentos.aplicar_imagens:
            linhas = criar_plano_imagens(resultado, encontrados, cliente)
            gerar_plano_imagens(linhas, argumentos.saida_simulacao)
            totais = Counter(str(linha["status_planejado"]) for linha in linhas)
            if argumentos.aplicar_imagens:
                print("PRE-APLICACAO: plano conferido antes de qualquer escrita.")
            else:
                print("SIMULACAO: nenhuma alteracao foi enviada ao Bling.")
            print(f"Imagens novas que seriam adicionadas: {totais['ADICIONAR']}")
            print(f"Imagens que ja existem: {totais['JA_EXISTE']}")
            ignoradas = totais["IGNORADO_PRODUTO_NAO_ENCONTRADO"]
            bloqueadas = (
                len(linhas)
                - totais["ADICIONAR"]
                - totais["JA_EXISTE"]
                - ignoradas
            )
            print(f"Imagens ignoradas por produto nao encontrado: {ignoradas}")
            print(f"Imagens bloqueadas por outras inconsistencias: {bloqueadas}")
            print(f"Plano de imagens gerado: {argumentos.saida_simulacao}")

        if argumentos.aplicar_imagens:
            if not isinstance(cliente, BlingImagens):
                raise RuntimeError("Cliente de escrita nao foi inicializado.")
            aplicacao = aplicar_imagens_piloto(
                resultado,
                encontrados,
                cliente,
                argumentos.sku_piloto,
            )
            gerar_relatorio_aplicacao(aplicacao, argumentos.saida_aplicacao)
            print(f"Resultado da aplicacao piloto: {aplicacao.status}")
            print(f"Motivo: {aplicacao.motivo}")
            print(f"Relatorio de aplicacao gerado: {argumentos.saida_aplicacao}")
            if aplicacao.status == "ERRO_VERIFICACAO_POS_PATCH":
                return 1

    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    parser = criar_parser()
    try:
        return executar(parser.parse_args())
    except (ErroServidorImagens, ErroBling, ValueError) as erro:
        print(f"ERRO: {erro}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
