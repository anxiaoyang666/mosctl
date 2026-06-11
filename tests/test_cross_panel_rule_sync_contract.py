from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "app.py"
INDEX = ROOT / "remote-root" / "etc" / "mosdns" / "manager" / "templates" / "index.html"


def app_source():
    return APP.read_text(encoding="utf-8")


def index_source():
    return INDEX.read_text(encoding="utf-8")


class MosctlCrossPanelRuleSyncContractTest(unittest.TestCase):
    def test_sync_protocol_matches_mihomo_rule_ids(self):
        text = app_source()

        self.assertIn('SYNCABLE_RULE_IDS = {"force-cn", "force-nocn"}', text)
        self.assertIn('/api/rule-sync', text)
        self.assertIn('"rules": {rule_id: content}', text)
        self.assertIn("apply_synced_rules", text)
        self.assertIn("X-Mosdns-Sync-Token", text)

    def test_sync_copy_mentions_mihomo_panels(self):
        text = index_source()

        self.assertIn("mosctl / mihomo", text)
        self.assertIn("其他 mosctl / mihomo 面板地址", text)
        self.assertNotIn("其他 mosdns 面板", text)


if __name__ == "__main__":
    unittest.main()
