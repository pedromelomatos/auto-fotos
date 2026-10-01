import unittest
import tkinter as tk
import tempfile
from unittest.mock import Mock, patch

from gui import (
    AutoFotosGUI,
    linha_pode_ser_aplicada,
    mensagem_erro_amigavel,
    pasta_e_raiz_da_url,
    resumo_plano,
    totais_plano,
)
from servidor import DiretorioImagens, ResultadoColeta


class GuiHelpersTests(unittest.TestCase):
    def setUp(self):
        self.linhas = [
            {"status_planejado": "ADICIONAR"},
            {"status_planejado": "ADICIONAR"},
            {"status_planejado": "JA_EXISTE"},
            {"status_planejado": "IGNORADO_PRODUTO_NAO_ENCONTRADO"},
            {"status_planejado": "BLOQUEADO_SEQUENCIA_INCONSISTENTE"},
        ]

    def test_contabiliza_status_do_plano(self):
        totais = totais_plano(self.linhas)

        self.assertEqual(totais["ADICIONAR"], 2)
        self.assertEqual(totais["JA_EXISTE"], 1)
        self.assertEqual(totais["IGNORADO_PRODUTO_NAO_ENCONTRADO"], 1)

    def test_resumo_separa_ignoradas_de_bloqueadas(self):
        self.assertEqual(
            resumo_plano(self.linhas),
            "Novas: 2  |  Já cadastradas: 1  |  Não encontradas: 1  |  Com problemas: 1",
        )

    def test_aplicacao_so_e_habilitada_para_status_adicionar(self):
        self.assertTrue(linha_pode_ser_aplicada(self.linhas[0]))
        self.assertFalse(linha_pode_ser_aplicada(self.linhas[2]))

    def test_separa_pasta_inicial_da_raiz_do_servidor(self):
        pasta, raiz = pasta_e_raiz_da_url(
            "https://exemplo.test:85/catalogo/MAKITA/"
        )

        self.assertEqual(pasta, "MAKITA")
        self.assertEqual(raiz, "https://exemplo.test:85/catalogo/")

    def test_orienta_renovacao_quando_token_expira(self):
        mensagem = mensagem_erro_amigavel(
            RuntimeError("Token do Bling ausente, invalido ou expirado.")
        )

        self.assertIn("Configurações avançadas", mensagem)
        self.assertIn("Renovar token", mensagem)

    def test_orienta_fechar_relatorio_aberto(self):
        mensagem = mensagem_erro_amigavel(PermissionError("arquivo ocupado"))

        self.assertIn("Feche o arquivo no Excel", mensagem)

    def test_inicializacao_nao_renova_token_ainda_valido(self):
        interface = AutoFotosGUI.__new__(AutoFotosGUI)
        interface._validar_autorizacao_inicial = Mock()
        configuracao = object()

        with (
            patch("gui.carregar_configuracao", return_value=configuracao),
            patch("gui.token_precisa_renovacao", return_value=False),
            patch("gui.tokens_configurados", return_value=True),
        ):
            interface._inicializar()

        interface._validar_autorizacao_inicial.assert_called_once_with(configuracao)

    def test_inicializacao_agenda_renovacao_para_token_expirado(self):
        interface = AutoFotosGUI.__new__(AutoFotosGUI)
        interface.atualizar_pastas = Mock()
        interface._executar_em_segundo_plano = Mock()
        configuracao = object()
        resultado_token = object()

        with (
            patch("gui.carregar_configuracao", return_value=configuracao),
            patch("gui.token_precisa_renovacao", return_value=True),
            patch("gui.tokens_configurados", return_value=True),
            patch("gui.renovar", return_value=resultado_token) as renovar_mock,
        ):
            interface._inicializar()
            tarefa = interface._executar_em_segundo_plano.call_args.args[1]
            resultado, erro = tarefa()

        renovar_mock.assert_called_once_with(configuracao)
        self.assertIs(resultado, resultado_token)
        self.assertIsNone(erro)

    def test_inicializacao_avisa_quando_nao_ha_tokens(self):
        interface = AutoFotosGUI.__new__(AutoFotosGUI)
        interface._avisar_bling_nao_autorizado = Mock()
        interface.atualizar_pastas = Mock()

        with (
            patch("gui.carregar_configuracao", return_value=object()),
            patch("gui.token_precisa_renovacao", return_value=False),
            patch("gui.tokens_configurados", return_value=False),
        ):
            interface._inicializar()

        interface._avisar_bling_nao_autorizado.assert_called_once_with()
        interface.atualizar_pastas.assert_called_once_with()

    def test_reconhece_token_revogado_como_nova_autorizacao(self):
        self.assertTrue(
            AutoFotosGUI._erro_exige_nova_autorizacao(
                RuntimeError("Token do Bling ausente, invalido ou expirado.")
            )
        )
        self.assertFalse(
            AutoFotosGUI._erro_exige_nova_autorizacao(
                RuntimeError("Falha de rede ao consultar a API do Bling.")
            )
        )


class NavegacaoPastasTests(unittest.TestCase):
    def setUp(self):
        self.raiz = tk.Tk()
        self.raiz.withdraw()
        self.addCleanup(self.raiz.destroy)
        with patch("gui.load_dotenv"):
            self.interface = AutoFotosGUI(self.raiz)
        self.interface._invalidar_plano_por_troca = Mock()
        self.interface._executar_em_segundo_plano = Mock(
            side_effect=lambda descricao, tarefa, concluir: concluir(tarefa())
        )
        self.fabricante = "https://exemplo.test/VIAPOL/"
        self.interna = self.fabricante + "Linha%20A/"
        self.folha = self.interna + "Produtos/"
        self.listagens = {
            self.fabricante: (DiretorioImagens("Linha A", self.interna),),
            self.interna: (DiretorioImagens("Produtos", self.folha),),
            self.folha: (),
        }
        listar_patch = patch(
            "gui.listar_diretorios", side_effect=lambda url, **kwargs: self.listagens[url]
        )
        self.listar = listar_patch.start()
        self.addCleanup(listar_patch.stop)
        self.interface._mostrar_pastas((DiretorioImagens("VIAPOL", self.fabricante),))

    def escolher(self, nivel, opcao):
        self.interface.subpastas[nivel].seletor.current(opcao)
        self.interface._ao_trocar_subpasta(nivel)

    def test_abre_seletores_sucessivos_e_para_na_pasta_sem_subpastas(self):
        self.assertEqual(len(self.interface.subpastas), 1)
        self.assertEqual(self.interface.url.get(), self.fabricante)
        self.escolher(0, 1)
        self.assertEqual(len(self.interface.subpastas), 2)
        self.assertEqual(self.interface.url.get(), self.interna)
        self.escolher(1, 1)
        self.assertEqual(len(self.interface.subpastas), 2)
        self.assertEqual(self.interface.url.get(), self.folha)
        self.assertEqual([chamada.args[0] for chamada in self.listar.call_args_list],
                         [self.fabricante, self.interna, self.folha])

    def test_volta_a_raiz_de_cada_nivel_e_remove_seletores_descendentes(self):
        self.escolher(0, 1)
        self.escolher(1, 1)
        self.escolher(1, 0)
        self.assertEqual(self.interface.url.get(), self.interna)
        quadro_descendente = self.interface.subpastas[1].quadro
        self.escolher(0, 0)
        self.assertEqual(self.interface.url.get(), self.fabricante)
        self.assertEqual(len(self.interface.subpastas), 1)
        self.assertFalse(quadro_descendente.winfo_exists())
        self.interface._invalidar_plano_por_troca.assert_called_with("/VIAPOL/")

    def test_trocar_fabricante_limpa_caminho_anterior(self):
        self.escolher(0, 1)
        outra = "https://exemplo.test/MAKITA/"
        self.listagens[outra] = ()
        self.interface.urls_por_pasta["MAKITA"] = outra
        self.interface.pasta_remota.set("MAKITA")
        self.interface._ao_trocar_pasta()
        self.assertEqual(self.interface.url.get(), outra)
        self.assertEqual(self.interface.subpastas, [])

    def test_bloqueia_subpastas_durante_operacao(self):
        self.interface._definir_ocupado(True)
        self.assertEqual(str(self.interface.subpastas[0].seletor["state"]), "disabled")
        self.interface._definir_ocupado(False)
        self.assertEqual(str(self.interface.subpastas[0].seletor["state"]), "readonly")

    def test_simulacao_coleta_url_da_subpasta_ou_da_raiz_escolhida(self):
        self.escolher(0, 1)
        resultado = ResultadoColeta((), 0, ())
        with (
            tempfile.TemporaryDirectory() as saida,
            patch("gui.load_dotenv"),
            patch("gui.coletar_imagens", return_value=resultado) as coletar,
            patch("gui.gerar_csv"),
            patch("gui.BlingSomenteLeitura"),
            patch("gui.gerar_conferencia_bling"),
            patch("gui.criar_plano_imagens", return_value=[]),
            patch("gui.gerar_plano_imagens"),
        ):
            self.interface.pasta_saida.set(saida)
            self.interface.simular()
            self.assertEqual(coletar.call_args.args[0], self.interna)
            self.escolher(0, 0)
            self.interface.simular()
            self.assertEqual(coletar.call_args.args[0], self.fabricante)


if __name__ == "__main__":
    unittest.main()
