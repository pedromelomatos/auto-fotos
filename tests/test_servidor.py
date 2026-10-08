import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from servidor import (
    ProdutoImagens,
    analisar_nome_arquivo,
    codigo_bling,
    coletar_imagens,
    extrair_diretorios,
    extrair_links_imagens,
)


class CodigoBlingTests(unittest.TestCase):
    def test_acrescenta_sufixo(self):
        self.assertEqual(codigo_bling("PR8254"), "PR82540001")
        self.assertEqual(codigo_bling("re1234"), "RE12340001")

    def test_aplica_excecao_de_prefixo(self):
        self.assertEqual(codigo_bling("PR31597"), "RE315970001")

    def test_preserva_sku_mlb_sem_acrescentar_sufixo(self):
        self.assertEqual(codigo_bling("MLB5031544400"), "MLB5031544400")
        self.assertEqual(codigo_bling(" mlb5031544400 "), "MLB5031544400")

    def test_rejeita_codigo_invalido(self):
        for codigo in ("XX1234", "MLB", "MLB5031544400A", "MLB5031544400-01"):
            with self.subTest(codigo=codigo), self.assertRaises(ValueError):
                codigo_bling(codigo)


class ParserImagemTests(unittest.TestCase):
    def test_identifica_sku_mlb_completo_e_posicao(self):
        for nome in (
            "MLB5031544400-PRODUTO_01.jpg",
            "MLB5031544400_01.png",
            "mlb5031544400-PRODUTO_01.WEBP",
        ):
            with self.subTest(nome=nome):
                imagem = analisar_nome_arquivo(nome, f"https://exemplo.test/{nome}")
                self.assertIsNotNone(imagem)
                self.assertEqual(imagem.codigo_servidor, "MLB5031544400")
                self.assertEqual(imagem.codigo_bling, "MLB5031544400")
                self.assertEqual(imagem.posicao, 1)

    def test_ignora_nome_mlb_invalido(self):
        for nome in (
            "MLB-PRODUTO_01.jpg",
            "MLB5031544400A-PRODUTO_01.jpg",
            "MLB5031544400-PRODUTO_00.jpg",
        ):
            with self.subTest(nome=nome):
                self.assertIsNone(analisar_nome_arquivo(nome, f"https://exemplo.test/{nome}"))

    def test_identifica_produto_e_posicao(self):
        nome = "PR8254-DISCO DIAMANTADO MAKITA GRANITO 105X10X20MM D-44351_01.jpg"
        imagem = analisar_nome_arquivo(nome, f"https://exemplo.test/MAKITA/{nome}")
        self.assertIsNotNone(imagem)
        self.assertEqual(imagem.codigo_servidor, "PR8254")
        self.assertEqual(imagem.codigo_bling, "PR82540001")
        self.assertEqual(imagem.posicao, 1)

    def test_texto_central_nao_afeta_agrupamento(self):
        primeira = analisar_nome_arquivo(
            "PR32898-TEXTO A_01.jpg",
            "https://exemplo.test/MAKITA/a.jpg",
        )
        segunda = analisar_nome_arquivo(
            "PR32898-TEXTO COMPLETAMENTE DIFERENTE_02.jpg",
            "https://exemplo.test/MAKITA/b.jpg",
        )
        self.assertEqual(primeira.codigo_servidor, segunda.codigo_servidor)

    def test_aceita_nome_descritivo_sem_posicao(self):
        for nome in (
            "PR8254-DISCO.jpg",
            "PR36740001 - KIT 12 COLA DE SILICONE - DE FRENTE.jpg",
            "PR39153000- ESCADA AGATA 5 DEGRAUS.jpg",
            "PR4028201 - COLHER DE PEDREIRO - DE PE.jpg",
            "MLB5031544400-PRODUTO.jpg",
        ):
            with self.subTest(nome=nome):
                imagem = analisar_nome_arquivo(nome, f"https://exemplo.test/{nome}")
                self.assertIsNotNone(imagem)
                self.assertTrue(imagem.posicao_automatica)

    def test_nao_aceita_codigo_invalido_ou_posicao_zero_com_fallback(self):
        for nome in ("PR123A - PRODUTO.jpg", "PR123 - PRODUTO_00.jpg", "SEM SKU.jpg"):
            with self.subTest(nome=nome):
                self.assertIsNone(analisar_nome_arquivo(nome, f"https://exemplo.test/{nome}"))

    def test_automaticas_nao_escondem_falta_nas_posicoes_explicitas(self):
        produto = ProdutoImagens("PR1", "PR10001")
        for nome in ("PR1-A_03.jpg", "PR1-B.jpg"):
            produto.adicionar(analisar_nome_arquivo(nome, f"https://exemplo.test/{nome}"))
        produto.ordenar()
        self.assertEqual(produto.posicoes, [3, 4])
        self.assertEqual(produto.posicoes_faltantes, [1, 2])
        self.assertEqual(produto.status_sequencia, "NUMEROS_FALTANTES")

    def test_coleta_descritivas_em_ordem_estavel_com_urls_codificadas(self):
        nomes = [
            "PR98140001 - AVENTAL PVC BRANCO - DE TRÁS.jpg",
            "PR98140001 - AVENTAL PVC BRANCO.jpg",
            "PR98140001 - AVENTAL PVC BRANCO_01.jpg",
        ]
        resultados = []
        for ordem in (nomes, list(reversed(nomes))):
            sessao = Mock()
            sessao.get.return_value.text = "".join(f'<a href="{nome}">Foto</a>' for nome in ordem)
            resultado = coletar_imagens("https://exemplo.test/editado/", sessao=sessao,
                                        verificar_certificado=True)
            produto = resultado.produtos[0]
            self.assertEqual(resultado.arquivos_imagem_ignorados, ())
            self.assertEqual(produto.status_sequencia, "OK")
            self.assertEqual(produto.posicoes, [1, 2, 3])
            self.assertTrue(all(" " not in imagem.url for imagem in produto.imagens))
            self.assertIn("%C3%81", produto.imagens[1].url)
            resultados.append([(i.arquivo, i.posicao) for i in produto.imagens])
        self.assertEqual(resultados[0], resultados[1])

    def test_detecta_falta_e_conflito(self):
        produto = ProdutoImagens("PR1", "PR10001")
        for nome, url in (
            ("PR1-A_01.jpg", "https://exemplo.test/1.jpg"),
            ("PR1-B_03.jpg", "https://exemplo.test/3a.jpg"),
            ("PR1-C_03.jpg", "https://exemplo.test/3b.jpg"),
        ):
            produto.adicionar(analisar_nome_arquivo(nome, url))
        self.assertEqual(produto.posicoes_faltantes, [2])
        self.assertEqual(produto.posicoes_em_conflito, [3])
        self.assertEqual(produto.status_sequencia, "CONFLITO_POSICAO")


class ExtracaoHtmlTests(unittest.TestCase):
    def test_lista_somente_subdiretorios_imediatos_da_mesma_origem(self):
        html = """
        <a href="../">Pai</a>
        <a href="MAKITA/">Makita</a>
        <a href="BOSCH/">Bosch</a>
        <a href="BOSCH/">Bosch repetida</a>
        <a href="BOSCH/INTERNA/">Subpasta</a>
        <a href="arquivo.jpg">Arquivo</a>
        <a href="https://outro.test/DEWALT/">Externa</a>
        """

        diretorios = extrair_diretorios(html, "https://exemplo.test/")

        self.assertEqual(
            [(item.nome, item.url) for item in diretorios],
            [
                ("BOSCH", "https://exemplo.test/BOSCH/"),
                ("MAKITA", "https://exemplo.test/MAKITA/"),
            ],
        )

    def test_filtra_links_e_remove_duplicados(self):
        html = """
        <a href="PR10-PRODUTO_01.jpg">Imagem 1</a>
        <a href="PR10-PRODUTO_01.jpg">Duplicada</a>
        <a href="PR10-PRODUTO_02.pdf">PDF</a>
        <a href="SEM-CODIGO_01.jpg">Fora do padrao</a>
        <a href="https://outro.test/PR10-PRODUTO_02.jpg">Externa</a>
        """
        links, ignorados, examinados = extrair_links_imagens(
            html,
            "https://exemplo.test/MAKITA/",
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(ignorados, ["SEM-CODIGO_01.jpg"])
        self.assertEqual(examinados, 5)


if __name__ == "__main__":
    unittest.main()
