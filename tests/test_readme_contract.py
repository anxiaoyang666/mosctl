from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
README_ZH = ROOT / "README.zh-CN.md"


class MosctlReadmeContractTest(unittest.TestCase):
    def test_default_readme_is_english(self):
        text = README.read_text(encoding="utf-8")

        self.assertIn("[English](README.md) | [中文](README.zh-CN.md)", text)
        self.assertIn("## One-Click Install", text)
        self.assertIn("The installer prints the Web panel address", text)
        self.assertIn(
            'bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"',
            text,
        )
        self.assertIn(
            'bash -c "$(curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"',
            text,
        )

    def test_both_readmes_document_every_env_key(self):
        keys = (
            "WEB_SESSION_SECRET",
            "WEB_USER",
            "WEB_SECRET",
            "WEB_PORT",
            "MOSCTL_REPO_URL",
            "MOSCTL_BRANCH",
            "GH_PROXY",
            "MIHOMO_CONTROLLER",
            "MIHOMO_API_SECRET",
            "RULE_SYNC_ENABLED",
            "RULE_SYNC_TOKEN",
            "RULE_SYNC_PEERS",
            "BACKUP_KEEP_COUNT",
        )
        for path in (README, README_ZH):
            text = path.read_text(encoding="utf-8")
            for key in keys:
                self.assertRegex(text, rf"(?m)^- `{key}`[:：]", f"{path.name} lacks {key}")
            self.assertIn("ip_forward", text)

    def test_chinese_readme_exists(self):
        text = README_ZH.read_text(encoding="utf-8")

        self.assertIn("[English](README.md) | [中文](README.zh-CN.md)", text)
        self.assertIn("## 一键安装", text)
        self.assertIn("默认 Web 端口是 `7840`", text)
        self.assertIn(
            'bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mosctl/main/install.sh)"',
            text,
        )


if __name__ == "__main__":
    unittest.main()
