import tempfile
import unittest
from pathlib import Path

from servidor import (
    ProdutoImagens,
    analisar_nome_arquivo,
    codigo_bling,
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
            "MLB5031544400-PRODUTO.jpg",
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

    def test_ignora_nome_sem_posicao(self):
        self.assertIsNone(
            analisar_nome_arquivo(
                "PR8254-DISCO.jpg",
                "https://exemplo.test/MAKITA/PR8254-DISCO.jpg",
            )
        )

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
