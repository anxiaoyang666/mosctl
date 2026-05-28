from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "remote-root" / "etc" / "mosdns" / "templates" / "default.yaml"


def read_template():
    return TEMPLATE.read_text(encoding="utf-8")


def plugin_block(text, tag):
    pattern = re.compile(
        rf"(?ms)^  - tag: {re.escape(tag)}\n.*?(?=^  - tag: |\Z)"
    )
    match = pattern.search(text)
    assert match, f"missing plugin tag {tag}"
    return match.group(0)


def main_exec_order(text):
    block = plugin_block(text, "main_sequence")
    return re.findall(r"exec: \$(query_is_[a-z_]+)", block)


class DefaultTemplateOrderTest(unittest.TestCase):
    def test_force_rule_sets_are_separate_from_geosite_sets(self):
        text = read_template()

        self.assertIn("tag: force_cn", text)
        self.assertIn("tag: force_no_cn", text)
        self.assertIn('"/etc/mosdns/rules/force-cn.txt"', plugin_block(text, "force_cn"))
        self.assertIn('"/etc/mosdns/rules/force-nocn.txt"', plugin_block(text, "force_no_cn"))
        self.assertNotIn("force-cn.txt", plugin_block(text, "geosite_cn"))
        self.assertNotIn("force-nocn.txt", plugin_block(text, "geosite_no_cn"))

    def test_force_rules_run_before_regular_geosite_rules(self):
        text = read_template()

        self.assertEqual(
            main_exec_order(text),
            [
                "query_is_force_cn_domain",
                "query_is_force_no_cn_domain",
                "query_is_local_domain",
                "query_is_no_local_domain",
            ],
        )

    def test_force_cn_bypasses_cache(self):
        text = read_template()
        block = plugin_block(text, "query_is_force_cn_domain")

        self.assertIn("exec: $forward_local", block)
        self.assertNotIn("cached_local_sequence", block)


if __name__ == "__main__":
    unittest.main()
