from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from select_sticker import select


class StickerSelectionTests(unittest.TestCase):
    def test_persona_excludes_cute_and_unclassified_images(self):
        cute = self.sticker("a", {"expression_styles": ["cute"], "cuteness": 4})
        unknown = self.sticker("b")
        stoic = self.sticker("c", {"expression_styles": ["stoic"], "cuteness": 0})
        request = self.options(max_cuteness=1, prefer_style=["stoic"])
        self.assertEqual(select([cute, unknown, stoic], request)[0]["id"], "c")
        self.assertEqual(select([cute, unknown], request), [])
        request = self.options(prefer_style=["stoic"])
        self.assertEqual(select([cute, stoic], request)[0]["id"], "c")

    def options(self, **overrides):
        values = dict(intent="acknowledge", audience="team_private", task_state="received",
                      placement="after_text", target=None, max_risk=0, abstraction=(0, 4),
                      max_meme_barrier=1, max_context_dependence=2, context=[], humor=[],
                      warmth=None, expressiveness=None, text="", serious=False, limit=1)
        values.update(overrides)
        return SimpleNamespace(**values)

    def sticker(self, name, profile=None, **overrides):
        analysis = dict(main_intent="acknowledge", secondary_intents=[], confidence="high",
                        commitment="received", send_mode="standalone", risk_severity=0,
                        risk_tags=[], formality=1, intimacy=0, teasing=0, abstraction=0,
                        meme_barrier=0, context_dependence=0, expressiveness=1)
        analysis.update(overrides)
        return {"id": name, "path": name + ".png", "analysis_status": "visually_reviewed",
                "analysis": analysis, "selection_profile": profile}

    def test_explicit_acknowledgment(self):
        vague = self.sticker("a", {"response_mode": "implicit_ack"})
        exact = self.sticker("b", {"response_mode": "explicit_ack"})
        request = self.options(response_mode="explicit_ack", require_response_mode=True)
        self.assertEqual(select([vague, exact], request)[0]["id"], "b")
        self.assertEqual(select([vague], request), [])

    def test_specific_occasion_and_legacy_generic(self):
        morning = self.sticker("a", {"occasion": "morning"}, main_intent="greet", commitment="none")
        generic = self.sticker("b", main_intent="greet", commitment="none")
        self.assertEqual(select([morning, generic], self.options(intent="greet"))[0]["id"], "b")
        request = self.options(intent="greet", occasion="morning", require_occasion=True)
        self.assertEqual(select([morning, generic], request)[0]["id"], "a")

    def test_action_scope_and_task_state_are_independent(self):
        buying = self.sticker("a", {"action_scope": "purchase"}, main_intent="accept_task", commitment="accept")
        attending = self.sticker("b", {"action_scope": "attend"}, main_intent="accept_task", commitment="accept")
        request = self.options(intent="accept_task", task_state="accept", action_scope="attend", require_action_scope=True)
        self.assertEqual(select([buying, attending], request)[0]["id"], "b")
        request.task_state = "received"
        self.assertEqual(select([buying, attending], request), [])

    def test_explicit_expression_limit_preserves_single_placement(self):
        loud = self.sticker("a", expressiveness=4, send_mode="reaction")
        quiet = self.sticker("b", expressiveness=2, send_mode="reaction")
        request = self.options(placement="reaction", max_expressiveness=2)
        self.assertEqual(select([loud, quiet], request)[0]["id"], "b")
        request.placement = "after_text"
        self.assertEqual(select([loud, quiet], request), [])


if __name__ == "__main__":
    unittest.main()
