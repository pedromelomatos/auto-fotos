import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from configuracao_local import caminho_env, pasta_aplicacao
from oauth_bling import carregar_configuracao, salvar_tokens
from dotenv import dotenv_values


class ConfiguracaoExecutavelTests(unittest.TestCase):
    def test_oauth_le_e_atualiza_env_ao_lado_do_exe_independente_do_diretorio(self):
        with tempfile.TemporaryDirectory() as temporaria:
            pasta = Path(temporaria).resolve()
            arquivo = pasta / ".env"
            arquivo.write_text("BLING_CLIENT_ID=cliente-teste\nBLING_CLIENT_SECRET=segredo-teste\n", encoding="utf-8")
            with patch("sys.frozen", True, create=True), patch("sys.executable", str(pasta / "AutoFotos.exe")):
                self.assertEqual(pasta_aplicacao(), pasta)
                self.assertEqual(caminho_env(), arquivo)
                configuracao = carregar_configuracao()
                self.assertEqual(configuracao.arquivo_env, arquivo)
                salvar_tokens(configuracao, {"access_token": "teste-access", "refresh_token": "teste-refresh", "expires_in": 3600})
            valores = dotenv_values(arquivo)
            self.assertEqual(valores["BLING_CLIENT_SECRET"], "segredo-teste")
            self.assertEqual(valores["BLING_ACCESS_TOKEN"], "teste-access")
            self.assertEqual(valores["BLING_REFRESH_TOKEN"], "teste-refresh")
