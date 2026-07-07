"""
Minimal stand-in for google-genai — enough for ai_service.py to import
AND (opt-in) to actually exercise its chat/generation flow for real.

By default every call still raises, exactly like before, so a test that
doesn't expect a real Gemini call fails loudly instead of silently doing
something unexpected. Tests that DO want to exercise generate_response /
core-memory extraction set `client.canned_chat_reply` and/or
`client.canned_model_response` first (see test_ai_service.py for
examples) — this keeps test_ai_service.py's original pure-logic checks
(which never touch Client at all) completely unaffected.

Every chats.create() / models.generate_content() call is logged on the
client instance (session_creation_log / model_call_log) so a test can
inspect exactly what was assembled and sent, which is the main thing
worth verifying about the per-user session/memory rework.
"""


class _FakeResponse:
    def __init__(self, text):
        self.text = text
        self.candidates = []
        self.parts = []
        self.prompt_feedbacks = None


class _FakeChatSession:
    def __init__(self, client, history):
        self._client = client
        self.history = history

    def send_message(self, message):
        if self._client.canned_chat_reply is None:
            raise RuntimeError(
                "Real Gemini calls are not exercised in these offline tests "
                "(set client.canned_chat_reply to enable)"
            )
        return _FakeResponse(text=self._client.canned_chat_reply)


class _FakeChats:
    def __init__(self, client):
        self._client = client

    def create(self, model=None, history=None, config=None):
        self._client.session_creation_log.append(
            {"model": model, "history": history, "config": config}
        )
        return _FakeChatSession(self._client, history)


class _FakeModels:
    def __init__(self, client):
        self._client = client

    def generate_content(self, model=None, contents=None, config=None):
        self._client.model_call_log.append(
            {"model": model, "contents": contents, "config": config}
        )
        canned = self._client.canned_model_response
        if canned is None:
            raise RuntimeError(
                "Real Gemini calls are not exercised in these offline tests "
                "(set client.canned_model_response to enable)"
            )
        text = canned(contents) if callable(canned) else canned
        return _FakeResponse(text=text)


class Client:
    def __init__(self, *args, **kwargs):
        self.chats = _FakeChats(self)
        self.models = _FakeModels(self)
        self.session_creation_log = []
        self.model_call_log = []
        # None (the default) means "this call shouldn't happen in this
        # test" and raises loudly. Set to a string for a fixed canned
        # reply, or a callable(contents) -> str to vary the response
        # based on what was actually sent (e.g. telling an extraction
        # call apart from a consolidation call).
        self.canned_chat_reply = None
        self.canned_model_response = None
