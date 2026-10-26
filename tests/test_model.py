"""NemotronModel request shape and response handling, against a stubbed HTTP session."""
import os
import unittest
from dataclasses import replace
from unittest import mock

from agent.config import ModelConfig
from agent.model import NemotronModel

_ENV = {"NEMOTRON_BASE_URL": "https://example.invalid/v1", "NEMOTRON_API_KEY": "k", "NEMOTRON_MODEL_ID": "m"}


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code, self._payload, self.headers = status, payload, {}

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payload):
        self.payload, self.bodies = payload, []

    def post(self, url, headers=None, json=None, timeout=None):
        self.bodies.append(json)
        return _Resp(self.payload)


def _reply(content, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


class NemotronModelTest(unittest.TestCase):
    def _model(self, payload, config=None):
        session = _Session(payload)
        with mock.patch.dict(os.environ, _ENV):
            model = NemotronModel(config or ModelConfig(), session=session)
        return model, session

    def test_thinking_flag_is_sent_by_default(self):
        model, session = self._model(_reply('{"type":"decision"}'))
        self.assertEqual(model.chat([{"role": "user", "content": "x"}], response_json=True), '{"type":"decision"}')
        body = session.bodies[0]
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_thinking_flag_omitted_when_none(self):
        model, session = self._model(_reply("ok"), replace(ModelConfig(), enable_thinking=None))
        model.chat([{"role": "user", "content": "x"}])
        self.assertNotIn("chat_template_kwargs", session.bodies[0])

    def test_empty_truncated_answer_raises(self):
        # A cut-off or empty completion is not a transport failure, so the model does
        # not raise ModelError. It returns the reply with truncated=True (empty content,
        # finish_reason "length"). The policy then sends one correction — "Your previous
        # reply was empty or cut off..." — and lets the model answer again, instead of
        # an exception forcing an immediate hold.
        model, _ = self._model(_reply("", finish="length"))
        result = model.chat_full([{"role": "user", "content": "x"}])
        self.assertTrue(result.truncated)
        self.assertEqual(result.content, "")
        self.assertEqual(result.finish_reason, "length")

    def test_think_block_is_stripped(self):
        model, _ = self._model(_reply("<think>plan</think>answer"))
        self.assertEqual(model.chat([{"role": "user", "content": "x"}]), "answer")


if __name__ == "__main__":
    unittest.main()
