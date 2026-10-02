import unittest
from io import BytesIO
from unittest.mock import Mock, patch

import requests
from PIL import Image

from previa import carregar_previa


class CarregamentoPreviaTests(unittest.TestCase):
    def resposta(self, dados):
        resposta = Mock()
        resposta.__enter__ = Mock(return_value=resposta)
        resposta.__exit__ = Mock(return_value=False)
        resposta.iter_content.return_value = [dados]
        return resposta

    def test_formatos_do_catalogo_preservam_proporcao(self):
        for formato in ("JPEG", "PNG", "WEBP"):
            with self.subTest(formato=formato):
                dados = BytesIO()
                Image.new("RGB", (1200, 600), "blue").save(dados, format=formato)
                resposta = self.resposta(dados.getvalue())
                with patch("previa.requests.get", return_value=resposta) as obter:
                    imagem = carregar_previa("https://exemplo.test/foto", False)
                self.assertEqual(imagem.size, (520, 260))
                self.assertEqual(imagem.mode, "RGBA")
                obter.assert_called_once_with("https://exemplo.test/foto", stream=True,
                                              timeout=(3, 8), verify=False)
                resposta.__exit__.assert_called_once()

    def test_respeita_orientacao_exif(self):
        foto = Image.new("RGB", (600, 300), "green")
        exif = Image.Exif()
        exif[274] = 6
        dados = BytesIO()
        foto.save(dados, format="JPEG", exif=exif)
        with patch("previa.requests.get", return_value=self.resposta(dados.getvalue())):
            imagem = carregar_previa("https://exemplo.test/foto", True)
        self.assertEqual(imagem.size, (160, 320))

    def test_interrompe_download_grande_e_fecha_resposta(self):
        resposta = self.resposta(b"123456")
        with patch("previa.requests.get", return_value=resposta), patch("previa.LIMITE_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "muito grande"):
                carregar_previa("https://exemplo.test/foto", True)
        resposta.__exit__.assert_called_once()

    def test_rejeita_dimensoes_excessivas_antes_de_decodificar(self):
        dados = BytesIO()
        Image.new("RGB", (100, 100)).save(dados, format="PNG")
        with (patch("previa.requests.get", return_value=self.resposta(dados.getvalue())),
              patch("previa.LIMITE_PIXELS", 9000)):
            with self.assertRaisesRegex(ValueError, "muito grande"):
                carregar_previa("https://exemplo.test/foto", True)

    def test_propaga_falha_http_sem_tentar_abrir_imagem(self):
        resposta = self.resposta(b"pagina de erro")
        resposta.raise_for_status.side_effect = requests.HTTPError("404")
        with patch("previa.requests.get", return_value=resposta):
            with self.assertRaises(requests.HTTPError):
                carregar_previa("https://exemplo.test/foto", True)
        resposta.iter_content.assert_not_called()

    def test_download_tem_limite_total_de_tempo(self):
        with (patch("previa.requests.get", return_value=self.resposta(b"x")),
              patch("previa.time.monotonic", side_effect=[0, 16])):
            with self.assertRaises(TimeoutError):
                carregar_previa("https://exemplo.test/foto", True)
