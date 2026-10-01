import argparse
import unittest
from unittest.mock import Mock

from bling import ProdutoBling
from main import (
    aplicar_imagens_piloto,
    criar_plano_imagens,
    validar_configuracao_escrita,
)
from servidor import ImagemProduto, ProdutoImagens, ResultadoColeta


def produto_com_imagem(codigo_servidor: str, codigo_bling: str) -> ProdutoImagens:
    produto = ProdutoImagens(codigo_servidor, codigo_bling)
    produto.adicionar(
        ImagemProduto(
            codigo_servidor=codigo_servidor,
            codigo_bling=codigo_bling,
            posicao=1,
            arquivo=f"{codigo_servidor}-PRODUTO_01.jpg",
            url=f"https://exemplo.test/{codigo_servidor}_01.jpg",
        )
    )
    return produto


class PlanoImagensTests(unittest.TestCase):
    def test_ignora_produto_ausente_e_continua_processando_os_demais(self):
        ausente = produto_com_imagem("PR1", "PR10001")
        encontrado = produto_com_imagem("PR2", "PR20001")
        resultado = ResultadoColeta(
            produtos=(ausente, encontrado),
            links_examinados=2,
            arquivos_imagem_ignorados=(),
        )
        resumo = ProdutoBling(123, "PR20001", "Produto encontrado", "A", dados={})
        detalhe = ProdutoBling(
            123,
            "PR20001",
            "Produto encontrado",
            "A",
            dados={},
        )
        cliente = Mock()
        cliente.obter_produto.return_value = detalhe

        linhas = criar_plano_imagens(
            resultado,
            {"PR10001": [], "PR20001": [resumo]},
            cliente,
        )

        self.assertEqual(
            [linha["status_planejado"] for linha in linhas],
            ["IGNORADO_PRODUTO_NAO_ENCONTRADO", "ADICIONAR"],
        )
        self.assertIn("processamento continuado", linhas[0]["motivo"])
        cliente.obter_produto.assert_called_once_with(123)


class AplicacaoImagensTests(unittest.TestCase):
    def setUp(self):
        self.produto = produto_com_imagem("PR2", "PR20001")
        self.resultado = ResultadoColeta((self.produto,), 1, ())
        self.resumo = ProdutoBling(123, "PR20001", "Produto", "A", dados={})

    @staticmethod
    def detalhe(*urls: str) -> ProdutoBling:
        return ProdutoBling(
            123,
            "PR20001",
            "Produto",
            "A",
            dados={
                "midia": {
                    "imagens": {
                        "externas": [{"link": url} for url in urls],
                    }
                }
            },
        )

    def test_preserva_existentes_aplica_nova_e_verifica(self):
        existente = "https://exemplo.test/existente.jpg"
        nova = "https://exemplo.test/PR2_01.jpg"
        cliente = Mock()
        cliente.obter_produto.side_effect = [
            self.detalhe(existente),
            self.detalhe(existente, nova),
        ]

        aplicacao = aplicar_imagens_piloto(
            self.resultado,
            {"PR20001": [self.resumo]},
            cliente,
            "pr20001",
        )

        self.assertEqual(aplicacao.status, "APLICADO_E_VERIFICADO")
        self.assertEqual(aplicacao.quantidade_novas, 1)
        cliente.atualizar_imagens_produto.assert_called_once_with(
            123,
            [existente, nova],
        )
        self.assertEqual(cliente.obter_produto.call_count, 2)

    def test_nao_envia_patch_quando_imagem_ja_existe(self):
        cliente = Mock()
        cliente.obter_produto.return_value = self.detalhe(
            "https://exemplo.test/PR2_01.jpg"
        )

        aplicacao = aplicar_imagens_piloto(
            self.resultado,
            {"PR20001": [self.resumo]},
            cliente,
            "PR20001",
        )

        self.assertEqual(aplicacao.status, "SEM_ALTERACAO")
        cliente.atualizar_imagens_produto.assert_not_called()
        cliente.obter_produto.assert_called_once_with(123)

    def test_sinaliza_produto_ausente_sem_enviar_patch(self):
        cliente = Mock()

        aplicacao = aplicar_imagens_piloto(
            self.resultado,
            {"PR20001": []},
            cliente,
            "PR20001",
        )

        self.assertEqual(aplicacao.status, "IGNORADO_PRODUTO_NAO_ENCONTRADO")
        cliente.atualizar_imagens_produto.assert_not_called()
        cliente.obter_produto.assert_not_called()

    def test_sinaliza_falha_na_conferencia_depois_do_patch(self):
        cliente = Mock()
        cliente.obter_produto.side_effect = [self.detalhe(), self.detalhe()]

        aplicacao = aplicar_imagens_piloto(
            self.resultado,
            {"PR20001": [self.resumo]},
            cliente,
            "PR20001",
        )

        self.assertEqual(aplicacao.status, "ERRO_VERIFICACAO_POS_PATCH")


class TravasEscritaTests(unittest.TestCase):
    def argumentos(self, **alteracoes):
        valores = {
            "aplicar_imagens": True,
            "sku_piloto": "PR20001",
            "confirmar_escrita": "APLICAR",
        }
        valores.update(alteracoes)
        return argparse.Namespace(**valores)

    def test_exige_sku_piloto(self):
        with self.assertRaisesRegex(ValueError, "--sku-piloto"):
            validar_configuracao_escrita(self.argumentos(sku_piloto=None))

    def test_exige_confirmacao_literal(self):
        with self.assertRaisesRegex(ValueError, "--confirmar-escrita APLICAR"):
            validar_configuracao_escrita(
                self.argumentos(confirmar_escrita="sim")
            )

    def test_normaliza_sku_confirmado(self):
        argumentos = self.argumentos(sku_piloto=" pr20001 ")

        validar_configuracao_escrita(argumentos)

        self.assertEqual(argumentos.sku_piloto, "PR20001")


if __name__ == "__main__":
    unittest.main()
