import argparse
import unittest
from unittest.mock import Mock, call

from bling import BlingImagens, ProdutoBling
from main import (
    aplicar_imagens_piloto,
    criar_plano_imagens,
    resolver_produtos_bling,
    validar_configuracao_escrita,
)
from servidor import ImagemProduto, ProdutoImagens, ResultadoColeta, coletar_imagens


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


class ResolucaoProdutosTests(unittest.TestCase):
    def test_prioriza_regra_tradicional_sem_consulta_adicional(self):
        produto = produto_com_imagem("PR8254", "PR82540001")
        resumo = ProdutoBling(123, "PR82540001", "Disco", "A", {})
        cliente = Mock()
        cliente.buscar_produtos_por_codigos.return_value = {"PR82540001": [resumo]}
        resultado, encontrados = resolver_produtos_bling(ResultadoColeta((produto,), 1, ()), cliente)
        cliente.buscar_produtos_por_codigos.assert_called_once_with(["PR82540001"])
        self.assertEqual(resultado.produtos[0].codigo_bling, "PR82540001")
        self.assertEqual(encontrados["PR82540001"], [resumo])

    def test_fallback_somente_para_ausentes_preserva_mlb_e_duplicidades(self):
        produtos = (
            produto_com_imagem("PR39153000", "PR391530000001"),
            produto_com_imagem("PR4028201", "PR40282010001"),
            produto_com_imagem("MLB123", "MLB123"),
        )
        duplicados = [ProdutoBling(i, "PR4028201", "Colher", "A", {}) for i in (1, 2)]
        cliente = Mock()
        cliente.buscar_produtos_por_codigos.side_effect = [
            {p.codigo_bling: [] for p in produtos},
            {"PR39153000": [], "PR4028201": duplicados},
        ]
        resultado, encontrados = resolver_produtos_bling(ResultadoColeta(produtos, 3, ()), cliente)
        self.assertEqual(cliente.buscar_produtos_por_codigos.call_args_list[1],
                         call(["PR39153000", "PR4028201"]))
        linhas = criar_plano_imagens(resultado, encontrados, cliente)
        self.assertEqual([linha["status_planejado"] for linha in linhas], [
            "IGNORADO_PRODUTO_NAO_ENCONTRADO", "BLOQUEADO_MULTIPLOS_PRODUTOS_ATIVOS",
            "IGNORADO_PRODUTO_NAO_ENCONTRADO",
        ])
        cliente.obter_produto.assert_not_called()

    def test_duplicidade_na_busca_principal_nao_dispara_fallback(self):
        produto = produto_com_imagem("PR2", "PR20001")
        cliente = Mock()
        cliente.buscar_produtos_por_codigos.return_value = {
            "PR20001": [ProdutoBling(i, "PR20001", "Produto", "A", {}) for i in (1, 2)],
        }
        resultado, encontrados = resolver_produtos_bling(ResultadoColeta((produto,), 1, ()), cliente)
        cliente.buscar_produtos_por_codigos.assert_called_once()
        self.assertEqual(criar_plano_imagens(resultado, encontrados, cliente)[0]["status_planejado"],
                         "BLOQUEADO_MULTIPLOS_PRODUTOS_ATIVOS")

    def test_sku_completo_sem_sufixo_e_usado_no_plano_e_na_aplicacao(self):
        sessao = Mock()
        sessao.get.return_value.text = '''
        <a href="PR36740001 - KIT 12 COLA - DE FRENTE.jpg">Frente</a>
        <a href="PR36740001 - KIT 12 COLA - DE LADO.jpg">Lado</a>
        '''
        coleta = coletar_imagens("https://exemplo.test/editado/", sessao=sessao,
                                 verificar_certificado=True)
        resumo = ProdutoBling(123, "PR36740001", "Kit 12 cola", "A", {})
        cliente = Mock()
        cliente.buscar_produtos_por_codigos.side_effect = [
            {"PR367400010001": []}, {"PR36740001": [resumo]},
        ]
        resultado, encontrados = resolver_produtos_bling(coleta, cliente)
        self.assertEqual(cliente.buscar_produtos_por_codigos.call_args_list,
                         [call(["PR367400010001"]), call(["PR36740001"])])
        self.assertEqual(coleta.produtos[0].codigo_bling, "PR367400010001")
        self.assertEqual(resultado.produtos[0].codigo_bling, "PR36740001")
        self.assertTrue(all(i.codigo_bling == "PR36740001" for i in resultado.produtos[0].imagens))
        urls = [i.url for i in resultado.produtos[0].imagens]
        depois = ProdutoBling(123, "PR36740001", "Kit 12 cola", "A", {
            "midia": {"imagens": {"externas": [{"link": url} for url in urls]}},
        })
        cliente.obter_produto.side_effect = [resumo, resumo, depois]
        plano = criar_plano_imagens(resultado, encontrados, cliente)
        self.assertTrue(all(linha["status_planejado"] == "ADICIONAR" for linha in plano))
        self.assertTrue(all(linha["codigo_bling"] == "PR36740001" for linha in plano))
        aplicacao = aplicar_imagens_piloto(resultado, encontrados, cliente, "PR36740001")
        self.assertEqual(aplicacao.status, "APLICADO_E_VERIFICADO")
        cliente.atualizar_imagens_produto.assert_called_once_with(123, urls)

    def test_agrupa_nomes_abreviados_e_completos_do_mesmo_sku(self):
        sessao = Mock()
        sessao.get.return_value.text = '''
        <a href="PR3674 - COLA.jpg">Antiga</a>
        <a href="PR36740001 - COLA - DE FRENTE.jpg">Nova</a>
        '''
        coleta = coletar_imagens("https://exemplo.test/editado/", sessao=sessao,
                                 verificar_certificado=True)
        cliente = Mock()
        cliente.buscar_produtos_por_codigos.return_value = {
            "PR36740001": [ProdutoBling(123, "PR36740001", "Cola", "A", {})],
            "PR367400010001": [],
        }
        resultado, _ = resolver_produtos_bling(coleta, cliente)
        cliente.buscar_produtos_por_codigos.assert_called_once()
        self.assertEqual(resultado.total_produtos, 1)
        self.assertEqual(resultado.total_imagens, 2)
        self.assertEqual(resultado.produtos[0].status_sequencia, "OK")
        self.assertEqual(resultado.produtos[0].posicoes, [1, 2])


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


class IntegracaoMlbTests(unittest.TestCase):
    def test_coleta_consulta_planeja_e_aplica_sku_mlb_sem_sufixo(self):
        sessao_servidor = Mock()
        sessao_servidor.get.return_value.text = """
        <a href="MLB5031544400-PRODUTO_02.jpg">MLB 2</a>
        <a href="PR8254-PRODUTO_01.jpg">PR</a>
        <a href="MLB5031544400-PRODUTO_01.jpg">MLB 1</a>
        <a href="RE1234-PRODUTO_01.jpg">RE</a>
        """
        resultado = coletar_imagens(
            "https://exemplo.test/",
            verificar_certificado=True,
            sessao=sessao_servidor,
        )
        self.assertEqual(
            [produto.codigo_bling for produto in resultado.produtos],
            ["MLB5031544400", "PR82540001", "RE12340001"],
        )
        self.assertEqual(resultado.arquivos_imagem_ignorados, ())
        produto_mlb = resultado.produtos[0]
        self.assertEqual([imagem.posicao for imagem in produto_mlb.imagens], [1, 2])
        existente = "https://exemplo.test/existente.jpg"
        finais = [existente, *(imagem.url for imagem in produto_mlb.imagens)]
        resumo = dict(id=123, codigo="MLB5031544400", nome="Produto MLB", situacao="A")
        detalhe_antes = {
            **resumo,
            "midia": {"imagens": {"externas": [{"link": existente}]}},
        }
        detalhe_depois = {
            **resumo,
            "midia": {"imagens": {"externas": [{"link": url} for url in finais]}},
        }
        cliente = BlingImagens("token-de-teste", intervalo_minimo=0)
        cliente._sessao.get = Mock(side_effect=[
            Mock(status_code=200, ok=True, json=Mock(return_value={"data": dados}))
            for dados in ([resumo], detalhe_antes, detalhe_antes, detalhe_depois)
        ])
        cliente._sessao.patch = Mock(return_value=Mock(
            status_code=200, ok=True, content=b"{}", json=Mock(return_value={}),
        ))

        encontrados = cliente.buscar_produtos_por_codigos(
            produto.codigo_bling for produto in resultado.produtos
        )
        parametros = cliente._sessao.get.call_args.kwargs["params"]
        self.assertIn(("codigos[]", "MLB5031544400"), parametros)
        self.assertNotIn(("codigos[]", "MLB50315444000001"), parametros)
        plano = criar_plano_imagens(resultado, encontrados, cliente)
        self.assertEqual(
            [linha["status_planejado"] for linha in plano],
            ["ADICIONAR", "ADICIONAR", "IGNORADO_PRODUTO_NAO_ENCONTRADO",
             "IGNORADO_PRODUTO_NAO_ENCONTRADO"],
        )

        aplicacao = aplicar_imagens_piloto(resultado, encontrados, cliente, "MLB5031544400")

        self.assertEqual(aplicacao.status, "APLICADO_E_VERIFICADO")
        self.assertEqual(aplicacao.codigo_bling, "MLB5031544400")
        self.assertEqual(aplicacao.quantidade_novas, 2)
        cliente._sessao.patch.assert_called_once_with(
            "https://api.bling.com.br/Api/v3/produtos/123",
            json={"midia": {"imagens": {"imagensURL": [{"link": url} for url in finais]}}},
            timeout=30.0,
        )


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
