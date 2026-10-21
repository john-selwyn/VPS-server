import os
from pathlib import Path
import runpy
from unittest.mock import patch

from django.test import SimpleTestCase


class UbuntuServerTemplateConfigurationTests(SimpleTestCase):
    def config(self, value):
        with patch.dict(os.environ, {
            "PYTHON_DOTENV_DISABLED": "1",
            "VPS_TEMPLATE_VMID": "104",
            "VPS_TEMPLATE_DEBIAN_13_VMID": "105",
            "VPS_TEMPLATE_WINDOWS_11_VMID": "106",
            "VPS_TEMPLATE_UBUNTU_SERVER_26_04_VMID": value,
        }), patch("proxmoxer.ProxmoxAPI"):
            return runpy.run_path(str(Path(__file__).with_name("proxmox.py")))

    def test_optional_configuration_preserves_existing_templates(self):
        config = self.config("")
        self.assertEqual(config["get_template_vmid"]("Ubuntu 26.04"), 104)
        with self.assertRaises(ValueError):
            config["get_template_vmid"]("Ubuntu Server 26.04")

    def test_server_selects_separate_template(self):
        config = self.config("107")  # Synthetic test fixture, not an allocated production VMID.
        self.assertEqual(config["get_template_vmid"]("Ubuntu Server 26.04"), 107)
        self.assertEqual(config["get_template_vmid"]("Ubuntu 26.04"), 104)

    def test_invalid_or_reused_template_is_rejected(self):
        for value in ("0", "-1", "invalid", "104", "105", "106"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.config(value)
