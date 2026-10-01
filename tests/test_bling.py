import unittest
from unittest.mock import Mock

from bling import BlingImagens, BlingSomenteLeitura, ProdutoBling


class BlingSomenteLeituraTests(unittest.TestCase):
    def test_verifica_acesso_com_consulta_minima(self):
        resposta = Mock()
        resposta.status_code = 200
        resposta.ok = True
        resposta.json.return_value = {"data": []}
        cliente = BlingSomenteLeitura("token-de-teste", intervalo_minimo=0)
        cliente._sessao.get = Mock(return_value=resposta)

        cliente.verificar_acesso()

        cliente._sessao.get.assert_called_once()
        _, kwargs = cliente._sessao.get.call_args
        self.assertEqual(kwargs["params"], [("criterio", 2), ("limite", 1)])

    def test_busca_produto_por_codigo_com_get(self):
        resposta = Mock()
        resposta.status_code = 200
        resposta.ok = True
        resposta.json.return_value = {
            "data": [
                {
                    "id": 123,
                    "codigo": "PR82540001",
                    "nome": "Disco diamantado",
                    "situacao": "A",
                }
            ]
        }

        cliente = BlingSomenteLeitura("token-de-teste")
        cliente._sessao.get = Mock(return_value=resposta)

        produto = cliente.buscar_produto_por_codigo("PR82540001")

        self.assertEqual(produto.id, 123)
        self.assertEqual(produto.codigo, "PR82540001")
        cliente._sessao.get.assert_called_once()
        _, kwargs = cliente._sessao.get.call_args
        self.assertIn(("codigos[]", "PR82540001"), kwargs["params"])
        self.assertIn(("criterio", 2), kwargs["params"])

    def test_extrai_urls_de_imagens_sem_duplicar(self):
        produto = ProdutoBling(
            id=123,
            codigo="PR1",
            nome="Produto",
            situacao="A",
            dados={
                "midia": {
                    "imagens": {
                        "externas": [
                            {"link": "https://exemplo.test/01.jpg"},
                            {"link": "https://exemplo.test/01.jpg"},
                        ],
                        "internas": [{"url": "https://exemplo.test/02.jpg"}],
                        "imagensURL": ["https://exemplo.test/03.jpg"],
                    }
                }
            },
        )

        self.assertEqual(
            produto.urls_imagens,
            (
                "https://exemplo.test/01.jpg",
                "https://exemplo.test/02.jpg",
                "https://exemplo.test/03.jpg",
            ),
        )


class BlingImagensTests(unittest.TestCase):
    def test_atualiza_lista_completa_de_imagens_por_patch(self):
        resposta = Mock()
        resposta.status_code = 200
        resposta.ok = True
        resposta.content = b"{}"
        resposta.json.return_value = {}
        cliente = BlingImagens("token-de-teste", intervalo_minimo=0)
        cliente._sessao.patch = Mock(return_value=resposta)

        cliente.atualizar_imagens_produto(
            123,
            [
                "https://exemplo.test/01.jpg",
                "https://exemplo.test/02.jpg",
                "https://exemplo.test/01.jpg",
            ],
        )

        cliente._sessao.patch.assert_called_once_with(
            "https://api.bling.com.br/Api/v3/produtos/123",
            json={
                "midia": {
                    "imagens": {
                        "imagensURL": [
                            {"link": "https://exemplo.test/01.jpg"},
                            {"link": "https://exemplo.test/02.jpg"},
                        ]
                    }
                }
            },
            timeout=30.0,
        )

    def test_bloqueia_lista_vazia_para_nao_apagar_imagens(self):
        cliente = BlingImagens("token-de-teste", intervalo_minimo=0)
        cliente._sessao.patch = Mock()

        with self.assertRaisesRegex(ValueError, "nao pode ser vazia"):
            cliente.atualizar_imagens_produto(123, [])

        cliente._sessao.patch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
