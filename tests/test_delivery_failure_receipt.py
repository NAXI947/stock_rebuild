"""A rejected notification retains only safe provider evidence for diagnosis."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from modules.strategy_hermes.stage2.notifications import deliver_qq
from modules.strategy_hermes.stage2.outbox import DeliveryPermanent, dispatch


class FailureReceipt(unittest.TestCase):
    def test_terminal_failure_is_persisted_with_http_and_provider_code(self):
        event=SimpleNamespace(payload={"text":"saved report"},event_key="report:1",attempt=1)
        client=Mock()
        client.call.return_value={"status":"REJECTED","message_posts":1,"http_status":400,
                                  "provider_code":40054009,"access_token":"must-not-save"}
        with patch("modules.strategy_hermes.stage2.outbox.claim",return_value=event), \
             patch("modules.strategy_hermes.stage2.outbox.finish",return_value="FAILED") as finish:
            self.assertEqual(dispatch(Mock(),"octop",lambda e:deliver_qq(Mock(),client,e)),"FAILED")
            self.assertEqual(finish.call_args.kwargs["receipt"],{
                "status":"REJECTED","message_posts":1,"http_status":400,"provider_code":40054009})
            self.assertTrue(finish.call_args.kwargs["terminal"])
            client.call.assert_called_once()
